"""Read-only API contract tests for the V13 observability endpoints."""

from __future__ import annotations

import asyncio
import copy
import types
import unittest

from brain.goal_system import Goal, GoalSystem
from brain.task_execution import TaskExecutionLedger
from brain.task_scheduler import LongTermTaskScheduler

try:  # The core test suite can run without the optional HTTP dependencies.
    import api.main as api_main
except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
    if exc.name != "fastapi":
        raise
    api_main = None


class _NoSyncScheduler(LongTermTaskScheduler):
    """Fail loudly if a GET endpoint attempts a synchronizing write pass."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sync_calls = 0

    def sync(self, *args, **kwargs):  # pragma: no cover - failure path
        self.sync_calls += 1
        raise AssertionError("V13 GET must not call scheduler.sync")


@unittest.skipIf(api_main is None, "FastAPI is not installed in this interpreter")
class V13ReadOnlyApiTests(unittest.TestCase):
    def setUp(self):
        self.goals = GoalSystem()
        self.goal = Goal(
            id="legacy-goal",
            drive="curiosity",
            description="检查只读查询不会初始化任务元数据",
            priority=0.4,
            deadline_ticks=100,
        )
        self.assertTrue(self.goals.add_goal(self.goal))

        self.scheduler = _NoSyncScheduler(max_queue=4)
        # A stale pointer and an unconfigured legacy goal are deliberately
        # retained: sync would clear/normalize both and therefore make this
        # test sensitive to accidental writes.
        self.scheduler.running_goal_id = "missing-goal"
        self.scheduler.last_tick = 7
        self.ledger = TaskExecutionLedger(max_plans=4, max_steps_per_plan=4)
        self.plan = self.ledger.create_plan(
            "只读 API 计划",
            goal_id=self.goal.id,
            steps=[
                {
                    "id": "step-read",
                    "description": "读取内部记录",
                    "action_type": "call_tool",
                    "tool_name": "memory_search",
                }
            ],
            current_tick=7,
        )
        self.assertIsNotNone(self.plan)

        stem = types.SimpleNamespace(
            goal_system=self.goals,
            state=types.SimpleNamespace(total_ticks=42),
            task_scheduler=self.scheduler,
            task_execution=self.ledger,
            learning_feedback=None,
        )
        self.brain = types.SimpleNamespace(brain_stem=stem)
        self.previous_brain = api_main._brain
        self.previous_bridge = api_main._bridge
        api_main._brain = self.brain
        api_main._bridge = None

    def tearDown(self):
        api_main._brain = self.previous_brain
        api_main._bridge = self.previous_bridge

    def _call(self, coroutine):
        return asyncio.run(coroutine)

    def test_v13_get_routes_do_not_mutate_scheduler_or_goal_state(self):
        goal_before = copy.deepcopy(self.goal.to_dict())
        scheduler_before = copy.deepcopy(
            {
                key: value
                for key, value in self.scheduler.__dict__.items()
                if key != "sync_calls"
            }
        )
        ledger_before = copy.deepcopy(self.ledger.snapshot())

        listing = self._call(api_main.get_v13_tasks())
        detail = self._call(api_main.get_v13_task(self.plan.id))
        metrics = self._call(api_main.get_v13_metrics())

        self.assertTrue(listing["enabled"])
        self.assertEqual(detail["plan"]["id"], self.plan.id)
        self.assertIn("execution", metrics)
        self.assertEqual(self.scheduler.sync_calls, 0)
        self.assertEqual(self.goal.to_dict(), goal_before)
        self.assertEqual(
            {
                key: value
                for key, value in self.scheduler.__dict__.items()
                if key != "sync_calls"
            },
            scheduler_before,
        )
        self.assertEqual(self.ledger.snapshot(), ledger_before)

    def test_v13_listing_does_not_evict_an_overfull_legacy_queue(self):
        """A GET must not enforce capacity; eviction belongs to sync/tick."""
        self.scheduler.max_queue = 1
        second = Goal(
            id="second-goal",
            drive="curiosity",
            description="队列溢出但仍只读",
            priority=0.1,
            deadline_ticks=100,
        )
        self.assertTrue(self.goals.add_goal(second))
        before_statuses = (self.goal.status, second.status)
        before_evictions = self.scheduler.total_evicted

        result = self._call(api_main.get_v13_tasks())

        self.assertEqual((self.goal.status, second.status), before_statuses)
        self.assertEqual(self.scheduler.total_evicted, before_evictions)
        self.assertEqual(result["scheduler"]["queue_size"], 1)
        self.assertEqual(self.scheduler.sync_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
