"""V13 bounded task execution and verification tests."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from pathlib import Path

from agent.tool_registry import ToolDef, ToolRegistry
from agent_bridge import AgentBridge
from brain.brain_stem import BrainStem
from brain.goal_system import Goal, GoalStatus
from brain.task_execution import (
    ActionStatus,
    DeterministicOutcomeEvaluator,
    OutcomeQuality,
    PlanStatus,
    StepStatus,
    TaskExecutionLedger,
)
from storage.database import MemoryStore, StateStore, init_db


class TaskExecutionLedgerTests(unittest.TestCase):
    def test_bounded_decomposition_dependency_and_cycle_guards(self):
        ledger = TaskExecutionLedger(max_steps_per_plan=3, max_depth=1)
        plan = ledger.create_plan(
            "有限计划",
            step_specs=[
                {"id": "root", "description": "根", "tool_name": "memory_search"},
                {
                    "id": "child",
                    "description": "子",
                    "parent_id": "root",
                    "dependencies": ["root"],
                },
            ],
        )
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 2)
        self.assertFalse(
            ledger.decompose_plan(
                plan.id,
                [
                    {"id": "x", "description": "x", "dependencies": ["y"]},
                    {"id": "y", "description": "y", "dependencies": ["x"]},
                ],
            )
        )
        self.assertFalse(
            ledger.decompose_plan(
                plan.id,
                [{"id": "too-deep", "description": "深", "depth": 2}],
            )
        )

    def test_get_plan_requires_both_identifiers_when_both_are_provided(self):
        ledger = TaskExecutionLedger()
        plan_a = ledger.create_plan("计划 A", goal_id="goal-a")
        plan_b = ledger.create_plan("计划 B", goal_id="goal-b")

        self.assertIs(ledger.get_plan(plan_a.id, "goal-a"), plan_a)
        self.assertIsNone(ledger.get_plan(plan_a.id, "goal-b"))
        self.assertIs(ledger.get_plan(goal_id="goal-b"), plan_b)

    def test_reusing_plan_id_is_idempotent_or_rejected_not_replaced(self):
        ledger = TaskExecutionLedger()
        original = ledger.create_plan(
            "不可替换计划",
            plan_id="stable-plan",
            goal_id="goal-a",
            step_specs=[{"id": "step-a", "description": "原步骤"}],
        )
        self.assertIsNotNone(original)
        self.assertIs(
            ledger.create_plan(
                "不可替换计划",
                plan_id="stable-plan",
                goal_id="goal-a",
                step_specs=[{"id": "different", "description": "不会覆盖"}],
            ),
            original,
        )
        self.assertIsNone(
            ledger.create_plan(
                "伪造替换",
                plan_id="stable-plan",
                goal_id="goal-b",
            )
        )
        self.assertEqual(original.objective, "不可替换计划")
        self.assertEqual([step.id for step in original.steps], ["step-a"])

    def test_evaluator_fails_closed_and_accepts_deterministic_schema(self):
        evaluator = DeterministicOutcomeEvaluator()
        unknown = evaluator.evaluate(
            {"summary": "工具说完成", "data": {}, "verified": True},
            {"required_fields": ["results"]},
            explicit_success=True,
            explicit_quality="verified",
        )
        self.assertIn(unknown.status, {OutcomeQuality.UNKNOWN, OutcomeQuality.FAILED})
        verified = evaluator.evaluate(
            {"summary": "结构化结果", "data": {"results": [{"title": "x"}]}},
            {"any_fields": ["results"]},
        )
        self.assertEqual(verified.status, OutcomeQuality.VERIFIED)
        simulated = evaluator.evaluate(
            {
                "summary": "离线",
                "data": {"results": []},
                "simulated": True,
            },
            {"any_fields": ["results"]},
        )
        self.assertEqual(simulated.status, OutcomeQuality.SIMULATED)
        failed = evaluator.evaluate(
            {"summary": "错误", "data": {"error": "boom"}},
            {"any_fields": ["results"]},
        )
        self.assertEqual(failed.status, OutcomeQuality.FAILED)

    def test_record_action_is_idempotent_for_reused_action_ids(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "重复动作",
            step_specs=[{"id": "s", "description": "步骤", "tool_name": "memory_search"}],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)

        action = ledger.record_action(
            plan.id,
            step.id,
            action_id="action-fixed",
            tool_name="memory_search",
            args={"query": "first"},
        )
        self.assertIsNotNone(action)
        self.assertEqual(ledger.stats["actions_recorded"], 1)

        duplicate = ledger.record_action(
            plan.id,
            step.id,
            action_id="action-fixed",
            tool_name="memory_search",
            args={"query": "first"},
        )
        self.assertIs(duplicate, action)
        self.assertEqual(ledger.stats["actions_recorded"], 1)
        self.assertEqual(ledger.actions["action-fixed"].tool_name, "memory_search")
        self.assertEqual(ledger.actions["action-fixed"].args_digest, action.args_digest)

        # A colliding id with different immutable semantics is rejected rather
        # than silently reusing the old action record.
        self.assertIsNone(
            ledger.record_action(
                plan.id,
                step.id,
                action_id="action-fixed",
                tool_name="web_search",
                args={"query": "second"},
            )
        )
        self.assertEqual(ledger.stats["actions_recorded"], 1)

        other = ledger.create_plan(
            "另一个动作",
            step_specs=[{"id": "s2", "description": "步骤 2", "tool_name": "file_read"}],
        )
        other_step = ledger.next_step(plan_id=other.id, current_tick=2)
        self.assertIsNone(
            ledger.record_action(
                other.id,
                other_step.id,
                action_id="action-fixed",
                tool_name="file_read",
            )
        )

    def test_observation_requires_action_to_cross_dispatch_boundary(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "行动生命周期",
            step_specs=[
                {
                    "id": "lifecycle-step",
                    "description": "先开始再观察",
                    "criteria": {"any_fields": ["results"]},
                }
            ],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)
        action = ledger.record_action(plan.id, step.id, action_id="lifecycle-action")
        self.assertIsNotNone(action)

        # A callback arriving before the bridge claims/starts the action is
        # stale or ambiguous.  It must not create evidence or advance the
        # step, even if its payload claims success.
        self.assertIsNone(
            ledger.record_observation(
                action.id,
                {"summary": "伪造成功", "data": {"results": [{"id": "x"}]}},
                tick=2,
            )
        )
        self.assertEqual(action.status, ActionStatus.PLANNED)
        self.assertEqual(step.status, StepStatus.RUNNING)
        self.assertEqual(ledger.stats["lifecycle_rejections"], 1)

        self.assertTrue(ledger.start_action(action.id, tick=3))
        outcome = ledger.record_observation(
            action.id,
            {"summary": "真实结构化结果", "data": {"results": [{"id": "x"}]}},
            tick=4,
        )
        self.assertEqual(outcome.status, OutcomeQuality.VERIFIED)

    def test_one_step_cannot_have_two_inflight_actions(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "单飞行动",
            step_specs=[{"id": "single-step", "description": "只能有一个"}],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)
        first = ledger.record_action(plan.id, step.id, action_id="first-action")
        self.assertIsNotNone(first)
        self.assertIsNone(
            ledger.record_action(plan.id, step.id, action_id="second-action")
        )
        self.assertEqual(len(ledger.actions), 1)
        self.assertEqual(ledger.stats["lifecycle_rejections"], 1)

    def test_empty_episode_scope_cannot_cancel_unrelated_actions(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "空作用域取消",
            step_specs=[{"id": "scoped-step", "description": "保持待确认"}],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)
        action = ledger.record_action(
            plan.id,
            step.id,
            episode_id="real-episode",
            action_id="scoped-action",
        )
        self.assertIsNotNone(action)

        # A missing episode anchor is not a wildcard.  Fail closed so a
        # malformed timeout/abort callback cannot cancel another lane.
        self.assertEqual(ledger.cancel_for_episode(""), 0)
        self.assertEqual(action.status, ActionStatus.PLANNED)
        self.assertEqual(plan.status, PlanStatus.ACTIVE)

    def test_direct_action_claim_cannot_bypass_dependencies(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "依赖边界",
            step_specs=[
                {"id": "pre", "description": "前置", "criteria": {"any_fields": ["results"]}},
                {
                    "id": "post",
                    "description": "后置",
                    "dependencies": ["pre"],
                    "criteria": {"any_fields": ["results"]},
                },
            ],
        )
        self.assertIsNotNone(plan)
        post = plan.get_step("post")
        self.assertIsNotNone(post)
        # Calling the low-level action API directly must still respect the
        # dependency graph; only ``next_step`` is allowed to claim a ready
        # step, and the predecessor must have a verified terminal result.
        self.assertIsNone(ledger.record_action(plan.id, post.id))
        self.assertEqual(ledger.stats["lifecycle_rejections"], 1)
        self.assertEqual(post.status, StepStatus.PENDING)

        pre = ledger.next_step(plan_id=plan.id, current_tick=1)
        pre_action = ledger.record_action(plan.id, pre.id)
        self.assertTrue(ledger.start_action(pre_action.id, tick=2))
        self.assertEqual(
            ledger.record_observation(
                pre_action.id,
                {"data": {"results": [{"id": "ok"}]}},
                tick=3,
            ).status,
            OutcomeQuality.VERIFIED,
        )
        post_action = ledger.record_action(plan.id, post.id)
        self.assertIsNotNone(post_action)

    def test_action_capacity_does_not_strand_a_direct_claim(self):
        ledger = TaskExecutionLedger(max_actions=1)
        first = ledger.create_plan(
            "占满动作账本",
            step_specs=[{"id": "first", "description": "first"}],
        )
        first_step = ledger.next_step(plan_id=first.id, current_tick=1)
        self.assertIsNotNone(ledger.record_action(first.id, first_step.id))

        second = ledger.create_plan(
            "第二个计划",
            step_specs=[{"id": "second", "description": "second"}],
        )
        second_step = ledger.next_step(plan_id=second.id, current_tick=2)
        self.assertIsNone(ledger.record_action(second.id, second_step.id))
        self.assertEqual(second.status, PlanStatus.PAUSED)
        self.assertEqual(second_step.status, StepStatus.PAUSED)

        # A direct caller that bypassed ``next_step`` must also be restored if
        # the action journal rejects its claim.
        third = ledger.create_plan(
            "第三个计划",
            step_specs=[{"id": "third", "description": "third"}],
        )
        third_step = third.steps[0]
        self.assertIsNone(ledger.record_action(third.id, third_step.id))
        self.assertEqual(third.status, PlanStatus.PENDING)
        self.assertEqual(third_step.status, StepStatus.PENDING)

    def test_retry_then_pause_and_snapshot_cancels_inflight(self):
        ledger = TaskExecutionLedger(max_events=20)
        plan = ledger.create_plan(
            "重试计划",
            step_specs=[
                {
                    "id": "s",
                    "description": "等待可靠证据",
                    "tool_name": "memory_search",
                    "max_retries": 1,
                    "criteria": {},
                }
            ],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)
        action = ledger.record_action(
            plan.id,
            step.id,
            tool_name="memory_search",
            args={"query": "x", "api_key": "do-not-store"},
        )
        self.assertEqual(action.status, ActionStatus.PLANNED)
        self.assertNotIn("do-not-store", action.to_dict()["args_summary"])
        self.assertTrue(ledger.start_action(action.id, tick=2))
        self.assertEqual(
            ledger.record_observation(
                action.id,
                {"summary": "无证据", "data": {}},
                tick=2,
            ).status,
            OutcomeQuality.UNKNOWN,
        )
        self.assertEqual(step.status, StepStatus.PENDING)
        step = ledger.next_step(plan_id=plan.id, current_tick=3)
        action = ledger.record_action(plan.id, step.id, tool_name="memory_search")
        self.assertTrue(ledger.start_action(action.id, tick=4))
        ledger.record_observation(
            action.id,
            {"summary": "仍无证据", "data": {}},
            tick=4,
        )
        self.assertEqual(plan.status, PlanStatus.PAUSED)
        restored = TaskExecutionLedger.from_snapshot(
            json.loads(json.dumps(ledger.snapshot(), ensure_ascii=False))
        )
        self.assertEqual(restored.get_plan(plan.id).status, PlanStatus.PAUSED)

        # A fresh in-flight action is cancelled on recovery and is never
        # replayed automatically.
        fresh = TaskExecutionLedger()
        p2 = fresh.create_plan("恢复", step_specs=[{"id": "s2", "description": "s2"}])
        s2 = fresh.next_step(plan_id=p2.id)
        a2 = fresh.record_action(p2.id, s2.id)
        restored2 = TaskExecutionLedger.from_snapshot(fresh.snapshot())
        self.assertEqual(restored2.actions[a2.id].status, ActionStatus.CANCELLED)
        self.assertEqual(restored2.get_plan(p2.id).status, PlanStatus.PAUSED)

    def test_safe_replan_waits_for_quiescence_and_is_bounded(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "安全重规划",
            step_specs=[
                {
                    "id": "replan-step",
                    "description": "先读内部记录",
                    "tool_name": "memory_search",
                    "criteria": {"any_fields": ["results"]},
                }
            ],
        )
        step = ledger.next_step(plan_id=plan.id, current_tick=1)
        action = ledger.record_action(
            plan.id,
            step.id,
            action_id="replan-action",
            tool_name="memory_search",
        )
        proposal = ledger.propose_replan(
            plan.id,
            step.id,
            "换用本地文件核验",
            alternative={
                "tool_name": "file_read",
                "action_type": "call_tool",
                "acceptance_criteria": {"required_fields": ["content_present"]},
            },
            tick=2,
        )
        self.assertTrue(proposal.safe)
        self.assertFalse(ledger.apply_replan(proposal.id, tick=2))
        self.assertEqual(step.tool_name, "memory_search")

        # After the ambiguous action is cancelled/paused, applying the same
        # safe proposal creates a fresh pending definition; it does not replay
        # the old action.
        self.assertTrue(ledger.cancel_action(action.id, tick=3))
        self.assertTrue(ledger.apply_replan(proposal.id, tick=3))
        self.assertEqual(action.status, ActionStatus.CANCELLED)
        self.assertEqual(step.status, StepStatus.PENDING)
        self.assertEqual(step.tool_name, "file_read")
        self.assertEqual(plan.status, PlanStatus.ACTIVE)

        step.replan_count = 2
        exhausted = ledger.propose_replan(
            plan.id,
            step.id,
            "不能无限重规划",
            alternative={"tool_name": "memory_search"},
            tick=4,
        )
        self.assertIsNotNone(exhausted)
        self.assertFalse(ledger.apply_replan(exhausted.id, tick=4))
        self.assertEqual(exhausted.status, "expired")

    def test_snapshot_restore_defers_refresh_until_after_full_load(self):
        ledger = TaskExecutionLedger()
        ledger.create_plan(
            "快照 A",
            step_specs=[{"id": "a", "description": "A"}],
        )
        ledger.create_plan(
            "快照 B",
            step_specs=[{"id": "b", "description": "B"}],
        )
        snapshot = json.loads(json.dumps(ledger.snapshot(), ensure_ascii=False))

        calls: list[tuple[str, int]] = []
        original_refresh = TaskExecutionLedger._refresh_plan

        def tracked_refresh(self, plan, current_tick: int = 0):
            calls.append((plan.id, current_tick))
            return original_refresh(self, plan, current_tick)

        TaskExecutionLedger._refresh_plan = tracked_refresh
        try:
            restored = TaskExecutionLedger.from_snapshot(snapshot)
        finally:
            TaskExecutionLedger._refresh_plan = original_refresh

        self.assertEqual(len(calls), len(restored.plans))
        self.assertEqual(
            {plan.id for plan in restored.plans.values()},
            {plan.id for plan in ledger.plans.values()},
        )

    def test_snapshot_restore_recovers_running_step_without_action_record(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "仅有 claim 的恢复",
            step_specs=[{"id": "claim-only", "description": "恢复 claim"}],
        )
        self.assertIsNotNone(plan)
        step = plan.steps[0]
        step.status = StepStatus.RUNNING
        plan.status = PlanStatus.ACTIVE
        plan.active_step_id = step.id

        restored = TaskExecutionLedger.from_snapshot(
            json.loads(json.dumps(ledger.snapshot(), ensure_ascii=False))
        )
        restored_plan = restored.get_plan(plan.id)
        self.assertIsNotNone(restored_plan)
        self.assertEqual(restored_plan.status, PlanStatus.PAUSED)
        self.assertEqual(restored_plan.active_step_id, "")
        self.assertEqual(restored_plan.steps[0].status, StepStatus.PAUSED)
        self.assertIn("未找到对应行动", restored_plan.steps[0].blocked_reason)
        self.assertEqual(restored.stats["recovered_actions"], 0)

    def test_evaluator_rejects_forged_verified_flag_without_real_evidence(self):
        evaluator = DeterministicOutcomeEvaluator()
        result = evaluator.evaluate(
            {
                "summary": "伪造的验证标记",
                "data": {"verified": True},
                "provenance": {"verified": False},
            },
            {"required_fields": ["verified"]},
            explicit_success=True,
            explicit_quality="verified",
        )
        self.assertEqual(result.status, OutcomeQuality.UNKNOWN)
        self.assertIsNone(result.success)

    def test_evaluator_does_not_trust_payload_provenance_or_success_claims(self):
        evaluator = DeterministicOutcomeEvaluator()
        provenance_claim = evaluator.evaluate(
            {
                "summary": "自称外部核验",
                "data": {
                    "results": [{"title": "claimed"}],
                    "provenance_verified": True,
                    "verified_sources": 1,
                },
            },
            {"required_fields": ["results"], "provenance_required": True},
            explicit_success=True,
            explicit_quality="verified",
        )
        self.assertNotEqual(provenance_claim.status, OutcomeQuality.VERIFIED)

        success_claim = evaluator.evaluate(
            {"summary": "完成", "data": {"success": True}},
            {"value_equals": {"success": True}},
            explicit_success=True,
            explicit_quality="verified",
        )
        self.assertEqual(success_claim.status, OutcomeQuality.UNKNOWN)
        self.assertIsNone(success_claim.success)

    def test_observation_id_collision_cannot_overwrite_evidence(self):
        ledger = TaskExecutionLedger()
        plan = ledger.create_plan(
            "观察关联",
            step_specs=[
                {"id": "one", "description": "one"},
                {"id": "two", "description": "two"},
            ],
        )
        first_step = ledger.next_step(plan_id=plan.id, current_tick=1)
        first_action = ledger.record_action(
            plan.id, first_step.id, action_id="observation-action-1"
        )
        self.assertTrue(ledger.start_action(first_action.id, tick=2))
        first = ledger.record_observation(
            first_action.id,
            {
                "id": "fixed-observation",
                "summary": "first",
                "data": {"results": [{"id": "a"}]},
            },
            tick=2,
            evaluate=False,
        )
        self.assertEqual(first.id, "fixed-observation")

        # A different action cannot replace the durable evidence under the
        # same observation ID, even if its payload looks successful.
        first_step.status = StepStatus.COMPLETED
        plan.active_step_id = ""
        plan.status = PlanStatus.PENDING
        second_step = ledger.next_step(plan_id=plan.id, current_tick=3)
        second_action = ledger.record_action(
            plan.id, second_step.id, action_id="observation-action-2"
        )
        self.assertTrue(ledger.start_action(second_action.id, tick=4))
        self.assertIsNone(
            ledger.record_observation(
                second_action.id,
                {
                    "id": "fixed-observation",
                    "summary": "forged replacement",
                    "data": {"results": [{"id": "b"}]},
                },
                tick=4,
                evaluate=False,
            )
        )
        self.assertEqual(ledger.observations["fixed-observation"].summary, "first")

    def test_empty_file_content_requires_explicit_truthy_content_flag(self):
        bridge = AgentBridge(None, ToolRegistry())
        observation = bridge._build_tool_observation(
            "file_read",
            json.dumps({"path": "notes.txt", "content": ""}, ensure_ascii=False),
            tool=None,
        )
        self.assertFalse(observation["data"]["content_present"])
        self.assertEqual(observation["data"]["content_length"], 0)

        evaluator = DeterministicOutcomeEvaluator()
        result = evaluator.evaluate(
            observation,
            {"value_equals": {"content_present": True}},
        )
        self.assertEqual(result.status, OutcomeQuality.FAILED)


class BrainTaskExecutionRollbackTests(unittest.TestCase):
    """A failed cross-component hand-off must not strand a claimed step."""

    def _fixture(self):
        stem = BrainStem(None, None)
        goal = Goal(
            id="goal-v13-rollback",
            drive="growth",
            description="回滚交接",
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
                    "id": "rollback-step",
                    "description": "需要安全交接",
                    "tool_name": "memory_search",
                    "criteria": {"any_fields": ["results"]},
                }
            ],
        )
        episode = stem.autonomy.begin(
            goal.id,
            goal.description,
            task_id=plan.id,
            tick=3,
        )
        return stem, goal, plan, episode

    def test_record_action_failure_restores_claim_and_releases_episode(self):
        stem, _goal, plan, episode = self._fixture()
        before = plan.to_dict()
        original = stem.task_execution.record_action
        stem.task_execution.record_action = lambda *args, **kwargs: None
        try:
            self.assertEqual(stem._queue_execution_step(_goal, episode, plan), (None, None))
        finally:
            stem.task_execution.record_action = original

        self.assertEqual(plan.to_dict(), before)
        self.assertEqual(plan.status, PlanStatus.PENDING)
        self.assertEqual(plan.steps[0].status, StepStatus.PENDING)
        self.assertEqual(plan.steps[0].attempts, 0)
        self.assertEqual(plan.consumed_ticks, 0)
        # The aborted lane no longer makes the scheduler report a permanently
        # busy episode, so the restored step can be claimed on a later tick.
        self.assertIsNone(stem.autonomy.active)
        self.assertIsNone(stem.task_scheduler.running_goal_id)

    def test_autonomy_plan_failure_cancels_action_and_restores_claim(self):
        stem, _goal, plan, episode = self._fixture()
        before = plan.to_dict()
        original = stem.autonomy.plan

        def reject_plan(*args, **kwargs):
            raise RuntimeError("autonomy backend unavailable")

        stem.autonomy.plan = reject_plan
        try:
            self.assertEqual(stem._queue_execution_step(_goal, episode, plan), (None, None))
        finally:
            stem.autonomy.plan = original

        self.assertEqual(plan.to_dict(), before)
        self.assertEqual(plan.steps[0].status, StepStatus.PENDING)
        self.assertEqual(plan.steps[0].attempts, 0)
        self.assertEqual(plan.consumed_ticks, 0)
        self.assertIsNone(stem.autonomy.active)
        actions = list(stem.task_execution.actions.values())
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].status, ActionStatus.CANCELLED)
        self.assertEqual(actions[0].outcome_id, "")

    def test_empty_ready_set_does_not_leave_a_busy_episode(self):
        stem, goal, plan, episode = self._fixture()
        # Simulate a torn/legacy snapshot in which the plan advertises an
        # active running step but no action can be safely delivered.
        step = plan.steps[0]
        step.status = StepStatus.RUNNING
        plan.status = PlanStatus.ACTIVE
        plan.active_step_id = step.id
        stem.task_scheduler.running_goal_id = goal.id

        intent, action = stem._queue_execution_step(goal, episode, plan)
        self.assertIsNone(intent)
        self.assertIsNone(action)
        stem._close_unqueued_execution_episode(goal, episode, plan)

        self.assertIsNone(stem.autonomy.active)
        self.assertEqual(stem.autonomy.last_closed.status, "aborted")
        self.assertEqual(plan.status, PlanStatus.PAUSED)
        self.assertEqual(step.status, StepStatus.PAUSED)
        self.assertIsNone(stem.task_scheduler.running_goal_id)

    def test_verified_completion_policy_cannot_weaken_plan_lane(self):
        stem, goal, plan, _episode = self._fixture()
        # The configuration knob is retained for legacy compatibility, but a
        # V13 plan must never project a hand-edited/unknown completion into a
        # successful Goal.  Durable evaluated evidence remains mandatory.
        stem.task_execution.require_verified_completion = False
        plan.steps[0].status = StepStatus.COMPLETED
        plan.status = PlanStatus.COMPLETED

        stem._sync_goal_with_plan(goal, plan)

        self.assertNotEqual(goal.status, GoalStatus.DONE)
        self.assertEqual(goal.status, GoalStatus.PAUSED)

    def test_restart_recovery_releases_scheduler_running_pointer(self):
        """A cancelled in-flight plan cannot leave a stale scheduler lane."""
        stem, goal, plan, _episode = self._fixture()
        # The scheduler snapshot may legitimately contain an ACTIVE goal while
        # the execution action is still awaiting the bridge.  Simulate a
        # power-loss snapshot at exactly that boundary.
        goal.status = GoalStatus.ACTIVE
        stem.task_scheduler.running_goal_id = goal.id
        step = plan.steps[0]
        step = stem.task_execution.next_step(plan_id=plan.id, current_tick=4)
        action = stem.task_execution.record_action(
            plan.id,
            step.id,
            action_id="restart-pointer-action",
            episode_id="episode-restart-pointer",
        )
        self.assertIsNotNone(action)

        snapshot = stem.state.snapshot()
        snapshot["goal_system"] = stem.goal_system.snapshot()
        snapshot["task_scheduler"] = stem.task_scheduler.snapshot(stem.goal_system)
        snapshot["task_execution"] = stem.task_execution.snapshot()
        restored = BrainStem(None, None)
        restored._restore_snapshot(snapshot)

        restored_plan = restored.task_execution.get_plan(plan.id)
        self.assertIsNotNone(restored_plan)
        self.assertEqual(restored_plan.status, PlanStatus.PAUSED)
        self.assertEqual(
            restored.task_execution.actions[action.id].status,
            ActionStatus.CANCELLED,
        )
        restored_goal = restored.goal_system.get_by_id(goal.id)
        self.assertIsNotNone(restored_goal)
        self.assertEqual(restored_goal.status, GoalStatus.PAUSED)
        self.assertIsNone(restored.task_scheduler.running_goal_id)

    def test_observation_capacity_failure_releases_active_episode(self):
        stem, goal, plan, episode = self._fixture()
        step = stem.task_execution.next_step(plan_id=plan.id, current_tick=4)
        action = stem.task_execution.record_action(
            plan.id,
            step.id,
            action_id="capacity-action",
            tool_name="memory_search",
            args={"query": "capacity"},
            episode_id=episode.id,
        )
        self.assertIsNotNone(action)
        self.assertTrue(
            stem.autonomy.plan(
                episode.id,
                "call_tool",
                "memory_search",
                tick=4,
                intent_id="capacity-intent",
                plan_id=plan.id,
                step_id=step.id,
                action_id=action.id,
            )
        )
        self.assertTrue(stem.autonomy.action_started(episode.id, tick=5))
        self.assertTrue(stem.task_execution.start_action(action.id, tick=5))
        stem.task_scheduler.running_goal_id = goal.id

        original_store = stem.task_execution._store

        def reject_observation(collection, key, value, limit):
            if collection == "observations":
                return False
            return original_store(collection, key, value, limit)

        stem.task_execution._store = reject_observation
        try:
            accepted = stem._record_autonomy_feedback(
                {
                    "source": "agent/tool/memory_search",
                    "text": "结构化结果",
                    "episode_id": episode.id,
                    "intent_id": "capacity-intent",
                    "goal_id": goal.id,
                    "plan_id": plan.id,
                    "step_id": step.id,
                    "action_id": action.id,
                    "_observation_token": stem._tool_observation_capability,
                    "tool_observation": {
                        "summary": "结果",
                        "data": {"results": [{"title": "x"}]},
                    },
                },
                True,
                "memory_search",
            )
        finally:
            stem.task_execution._store = original_store

        self.assertFalse(accepted)
        self.assertIsNone(stem.autonomy.active)
        self.assertEqual(stem.autonomy.last_closed.status, "aborted")
        self.assertEqual(action.status, ActionStatus.CANCELLED)
        self.assertEqual(plan.status, PlanStatus.PAUSED)
        self.assertIsNone(stem.task_scheduler.running_goal_id)


class BrainTaskExecutionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cognitive_io_cannot_hold_heartbeat(self):
        stem = BrainStem()
        stem.cognitive_timeout_sec = 0.02

        async def slow_retrieve(query, top_k=5):
            await asyncio.sleep(0.5)
            return {"results": [], "total_stored": 0}

        stem.hippocampus.retrieve = slow_retrieve
        _request_id, future = stem.submit_input("heartbeat timeout probe", source="user")
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(stem._tick(), timeout=0.8)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.8)
        self.assertTrue(future.done())
        self.assertTrue(stem.state.llm_error_count >= 1)
        self.assertIn("timeout", stem.state.last_error)

    async def test_rejected_correlated_feedback_cannot_train_other_subsystems(self):
        stem = BrainStem()
        goal = Goal(
            id="goal-v13-quarantine",
            drive="growth",
            description="拒绝越界反馈",
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
                    "id": "quarantine-step",
                    "description": "等待正确关联",
                    "tool_name": "memory_search",
                    "criteria": {"any_fields": ["results"]},
                }
            ],
        )
        episode = stem.autonomy.begin(
            goal.id,
            goal.description,
            task_id=plan.id,
            tick=1,
        )
        step = stem.task_execution.next_step(plan_id=plan.id, current_tick=1)
        action = stem.task_execution.record_action(
            plan.id,
            step.id,
            action_id="action-quarantine",
            tool_name="memory_search",
            args={"query": "safe"},
            episode_id=episode.id,
        )
        self.assertIsNotNone(action)
        self.assertTrue(
            stem.autonomy.plan(
                episode.id,
                "call_tool",
                "memory_search",
                tick=1,
                intent_id="intent-quarantine",
                plan_id=plan.id,
                step_id=step.id,
                action_id=action.id,
            )
        )
        self.assertTrue(stem.autonomy.action_started(episode.id, tick=2))
        self.assertTrue(stem.task_execution.start_action(action.id, tick=2))

        # The forged callback has a real episode/plan/step but an unrelated
        # action id.  If it reaches the ordinary input pipeline any of these
        # sentinels will fail the test; a quarantine must stop before them.
        forbidden = AssertionError("rejected feedback reached a learner")
        patches = [
            patch.object(
                stem.hippocampus,
                "retrieve",
                new=AsyncMock(side_effect=AssertionError("rejected feedback reached hippocampus")),
            ),
            patch.object(stem.amygdala, "evaluate", side_effect=forbidden),
            patch.object(stem.metacognition, "feed_outcome", side_effect=forbidden),
            patch.object(stem.metacognition, "feed_intent", side_effect=forbidden),
            patch.object(stem.metacognition, "update_cognitive_load", side_effect=forbidden),
            patch.object(stem.procedural_memory, "record_experience", side_effect=forbidden),
        ]
        if stem.predictive_layer is not None:
            patches.extend([
                patch.object(stem.predictive_layer, "build_expectation", side_effect=forbidden),
                patch.object(stem.predictive_layer, "observe_and_compute", side_effect=forbidden),
            ])
        if stem.reward_system is not None:
            patches.append(
                patch.object(stem.reward_system, "deliver_reward", side_effect=forbidden)
            )
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            request_id, future = stem.submit_input(
                "伪造的成功反馈",
                source="agent/tool/memory_search",
                episode_id=episode.id,
                intent_id="intent-quarantine",
                goal_id=goal.id,
                plan_id=plan.id,
                step_id=step.id,
                action_id="wrong-action",
                tool_observation={
                    "summary": "伪造成功",
                    "data": {"results": [{"title": "forged"}]},
                },
            )
            await stem._tick()

        self.assertTrue(future.done(), request_id)
        self.assertIs(stem.autonomy.active, episode)
        self.assertEqual(episode.status, "awaiting_feedback")
        self.assertEqual(action.status, ActionStatus.STARTED)
        self.assertEqual(stem.autonomy.total_orphan_feedback, 1)

    async def test_two_verified_steps_complete_goal_but_unknown_does_not(self):
        temp_dir = tempfile.mkdtemp(prefix="brain-v13-test-")
        state_store = memory_store = None
        stem = None
        try:
            db_path = str(Path(temp_dir) / "state.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            goal = Goal(
                id="goal-v13-two-step",
                drive="growth",
                description="验证两步任务",
                priority=0.9,
                deadline_ticks=100,
                status=GoalStatus.ACTIVE,
            )
            stem.goal_system._goals.append(goal)
            plan = stem.task_execution.create_plan(
                goal.description,
                goal_id=goal.id,
                step_specs=[
                    {
                        "id": "one",
                        "description": "内部核验",
                        "tool_name": "memory_search",
                        "criteria": {"any_fields": ["results"]},
                    },
                    {
                        "id": "two",
                        "description": "文件核验",
                        "tool_name": "file_read",
                        "criteria": {"required_fields": ["content_present"]},
                        "dependencies": ["one"],
                    },
                ],
            )
            tools = ToolRegistry()

            async def memory_search(args, context):
                return json.dumps({"results": [{"title": "内部记录"}]})

            async def file_read(args, context):
                return json.dumps({"path": "README.md", "content": "ok"})

            tools.register(
                ToolDef(
                    name="memory_search",
                    description="local read-only memory",
                    schema={},
                    call=memory_search,
                    is_read_only=True,
                )
            )
            tools.register(
                ToolDef(
                    name="file_read",
                    description="local read-only file",
                    schema={},
                    call=file_read,
                    is_read_only=True,
                )
            )
            bridge = AgentBridge(stem, tools)

            # Claim and complete first step.
            stem.state.total_ticks = 15
            stem.state.ticks_since_input = 14
            await stem._tick()
            first = await stem.intent_queue.get(timeout=0.1)
            await bridge._handle_call_tool(first)
            stem.state.total_ticks = 16
            stem.state.ticks_since_input = 15
            await stem._tick()
            self.assertEqual(plan.steps[0].status, StepStatus.COMPLETED)
            self.assertNotEqual(goal.status, GoalStatus.DONE)

            # Next safe boundary emits the dependency-ready second step.
            stem.state.total_ticks = 31
            stem.state.ticks_since_input = 14
            await stem._tick()
            second = await stem.intent_queue.get(timeout=0.1)
            self.assertEqual(second.step_id, "two")
            await bridge._handle_call_tool(second)
            stem.state.total_ticks = 32
            stem.state.ticks_since_input = 15
            await stem._tick()
            self.assertEqual(plan.status, PlanStatus.COMPLETED)
            self.assertEqual(goal.status, GoalStatus.DONE)
            self.assertEqual(
                stem.task_execution.metrics()["completed_plans"], 1
            )
            self.assertGreaterEqual(stem.learning_feedback.snapshot()["stats"]["total_positive"], 2)
        finally:
            if stem is not None and stem._task:
                await stem.stop()
            if state_store is not None:
                state_store.close()
            if memory_store is not None:
                memory_store.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
