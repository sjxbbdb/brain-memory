"""长期任务层的离线验收测试。"""

import json
import unittest

from brain.goal_system import Goal, GoalStatus, GoalSystem
from brain.task_scheduler import LongTermTaskScheduler, TaskTier


def make_goal(goal_id, drive="curiosity", priority=0.5, **kwargs):
    return Goal(
        id=goal_id,
        drive=drive,
        description=kwargs.pop("description", goal_id),
        priority=priority,
        deadline_ticks=kwargs.pop("deadline_ticks", 200),
        **kwargs,
    )


class LongTermTaskSchedulerTests(unittest.TestCase):
    def test_tier_order_and_safe_preemption(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=8)
        exploration = make_goal("explore", priority=0.99)
        user = make_goal("user", drive="growth", priority=0.1)
        maintenance = make_goal("maintenance", drive="self_preservation", priority=0.01)
        self.assertTrue(scheduler.register_goal(goals, exploration, current_tick=0))
        first = scheduler.claim_for_execution(goals, current_tick=1)
        self.assertEqual(first.id, "explore")
        self.assertTrue(scheduler.register_goal(
            goals, user, current_tick=0, tier=TaskTier.USER, source="creator"
        ))
        self.assertTrue(scheduler.register_goal(goals, maintenance, current_tick=0))

        maintenance_run = scheduler.select(goals, current_tick=2, episode_busy=False)
        self.assertEqual(maintenance_run.id, "maintenance")
        self.assertEqual(maintenance_run.task_tier, TaskTier.MAINTENANCE)
        self.assertEqual(exploration.status, GoalStatus.PAUSED)

        goals.mark_done("maintenance", "维护完成")
        scheduler.on_goal_terminal("maintenance")
        second = scheduler.claim_for_execution(goals, current_tick=2)
        self.assertEqual(second.id, "user")
        self.assertEqual(exploration.preemption_count, 1)
        self.assertEqual(scheduler.total_preemptions, 1)

    def test_episode_boundary_blocks_preemption(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=8)
        low = make_goal("low", priority=0.8)
        self.assertTrue(scheduler.register_goal(goals, low, current_tick=1))
        self.assertEqual(scheduler.claim_for_execution(goals, 1).id, "low")
        high = make_goal("high", drive="growth", priority=1.0)
        self.assertTrue(scheduler.register_goal(
            goals, high, current_tick=2, tier=TaskTier.USER, source="user"
        ))
        selected = scheduler.select(goals, current_tick=2, episode_busy=True)
        self.assertEqual(selected.id, "low")
        self.assertEqual(low.status, GoalStatus.ACTIVE)
        selected = scheduler.select(goals, current_tick=2, episode_busy=False)
        self.assertEqual(selected.id, "high")
        self.assertEqual(low.status, GoalStatus.PAUSED)

    def test_queue_eviction_preserves_higher_tiers(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=3)
        low_a = make_goal("low-a", priority=0.1)
        low_b = make_goal("low-b", priority=0.2)
        low_c = make_goal("low-c", priority=0.3)
        for goal in (low_a, low_b, low_c):
            self.assertTrue(scheduler.register_goal(goals, goal, current_tick=0))
        user = make_goal("user", drive="growth", priority=0.4)
        self.assertTrue(scheduler.register_goal(
            goals, user, current_tick=1, tier=TaskTier.USER, source="user"
        ))
        self.assertEqual(scheduler.total_evicted, 1)
        self.assertEqual(goals.get_by_id("low-a").status, GoalStatus.ABANDONED)
        self.assertIsNotNone(goals.get_by_id("user"))
        self.assertEqual(len(scheduler._queued(goals)), 3)

    def test_queue_eviction_keeps_the_current_execution_lane(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=2)
        running = make_goal("running", priority=0.2)
        queued = make_goal("queued", priority=0.1)
        self.assertTrue(scheduler.register_goal(goals, running, current_tick=0))
        self.assertTrue(scheduler.register_goal(goals, queued, current_tick=0))
        self.assertEqual(scheduler.claim_for_execution(goals, current_tick=1).id, "running")

        user = make_goal("user-lane", drive="growth", priority=0.9)
        self.assertTrue(scheduler.register_goal(
            goals, user, current_tick=2, tier=TaskTier.USER, source="user"
        ))
        self.assertEqual(running.status, GoalStatus.ACTIVE)
        self.assertEqual(queued.status, GoalStatus.ABANDONED)
        self.assertEqual(scheduler.running_goal_id, running.id)

    def test_budget_and_absolute_deadline_fail_safely(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=4)
        budgeted = make_goal("budget", budget_ticks=1, deadline_tick=100)
        self.assertTrue(scheduler.register_goal(goals, budgeted, current_tick=0))
        self.assertEqual(scheduler.claim_for_execution(goals, 1).id, "budget")
        self.assertIsNone(scheduler.select(goals, current_tick=2, episode_busy=False))
        self.assertEqual(budgeted.status, GoalStatus.FAILED)
        self.assertIn("预算", budgeted.result_note)

        deadline = make_goal("deadline", deadline_ticks=2, deadline_tick=3)
        self.assertTrue(scheduler.register_goal(goals, deadline, current_tick=0))
        self.assertIsNone(scheduler.select(goals, current_tick=3, episode_busy=False))
        self.assertEqual(deadline.status, GoalStatus.FAILED)
        self.assertIn("截止", deadline.result_note)

    def test_pause_resume_and_user_deduplication(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=4)
        first = scheduler.submit_user_goal(goals, "完成连续性检查", current_tick=0)
        second = scheduler.submit_user_goal(goals, "完成连续性检查", current_tick=4)
        self.assertIs(first, second)
        self.assertEqual(goals.total_generated, 1)
        self.assertEqual(first.task_tier, TaskTier.USER)
        self.assertTrue(scheduler.pause(goals, first.id, "稍后处理"))
        self.assertEqual(first.status, GoalStatus.PAUSED)
        self.assertTrue(scheduler.resume(goals, first.id))
        self.assertEqual(first.status, GoalStatus.PENDING)

    def test_snapshot_restores_policy_and_goal_metadata(self):
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=4)
        goal = scheduler.submit_user_goal(goals, "保存任务状态", current_tick=2)
        scheduler.claim_for_execution(goals, current_tick=3)
        snapshot = {
            "goal_system": goals.snapshot(),
            "task_scheduler": scheduler.snapshot(goals),
        }
        restored_goals = GoalSystem.from_snapshot(json.loads(json.dumps(snapshot["goal_system"])))
        restored_scheduler = LongTermTaskScheduler.from_snapshot(
            json.loads(json.dumps(snapshot["task_scheduler"]))
        )
        restored_scheduler.sync(restored_goals, current_tick=4)
        restored_goal = restored_goals.get_by_id(goal.id)
        self.assertEqual(restored_scheduler.running_goal_id, goal.id)
        self.assertEqual(restored_goal.task_tier, TaskTier.USER)
        self.assertEqual(restored_goal.budget_ticks, goal.budget_ticks)
        self.assertEqual(restored_goal.consumed_ticks, 1)

    def test_read_only_snapshot_does_not_normalize_or_evict(self):
        """A diagnostic snapshot must never perform the scheduler write pass."""
        goals = GoalSystem()
        scheduler = LongTermTaskScheduler(max_queue=1)
        legacy = make_goal("legacy", priority=0.2)
        self.assertTrue(goals.add_goal(legacy))
        scheduler.running_goal_id = "stale-pointer"
        scheduler.last_tick = 9
        before_goal = legacy.to_dict()
        before_scheduler = scheduler.snapshot()

        observed = scheduler.read_only_snapshot(goals)

        self.assertEqual(observed, scheduler.snapshot(goals))
        self.assertEqual(legacy.to_dict(), before_goal)
        # In particular, no lazy defaults, pointer cleanup, or queue
        # eviction may be triggered by a read-only call.
        self.assertEqual(scheduler.last_tick, 9)
        self.assertEqual(scheduler.running_goal_id, "stale-pointer")
        self.assertEqual(scheduler.snapshot(), before_scheduler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
