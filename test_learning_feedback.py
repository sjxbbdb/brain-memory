"""V13 verified-outcome learning boundary tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.autonomy import EpisodeRecord, EpisodeStatus
from brain.learning_feedback import (
    LearningDisposition,
    VerifiedLearningFeedback,
)
from brain.procedural_memory import ProceduralMemory
from brain.reward_system import RewardSystem
from brain.self_model import SelfModel
from storage.database import MemoryStore, init_db


def verified(outcome_id: str = "outcome-1", **overrides):
    result = {
        "outcome_id": outcome_id,
        "episode_id": "episode-1",
        "task_id": "task-1",
        "status": "completed",
        "success": True,
        "quality": "verified",
        "goal": "验证本地资料",
        "intent_type": "call_tool",
        "tool_name": "memory_search",
        "summary": "命中一条经过核验的资料",
        "verification_method": "local_schema_check",
    }
    result.update(overrides)
    return result


class VerifiedLearningFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.feedback = VerifiedLearningFeedback()
        self.procedural = ProceduralMemory()
        self.reward = RewardSystem()
        self.self_model = SelfModel()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "learning.db")
        init_db(self.db_path)
        self.memory = MemoryStore(self.db_path)
        self.drive_events = []

        class DriveRecorder:
            def __init__(inner_self, events):
                inner_self.events = events

            def record_verified_outcome(
                inner_self, activation, *, drive, success, current_tick
            ):
                inner_self.events.append((activation, drive, success, current_tick))

        self.drive = DriveRecorder(self.drive_events)

    def tearDown(self):
        self.memory.close()
        self.temp_dir.cleanup()

    def apply(self, outcome):
        return self.feedback.apply(
            outcome,
            procedural_memory=self.procedural,
            reward_system=self.reward,
            self_model=self.self_model,
            memory_store=self.memory,
            drive_engine=self.drive,
            activation="activation-sentinel",
            current_tick=17,
        )

    def test_verified_success_updates_all_safe_sinks(self):
        receipt = self.apply(verified())

        self.assertEqual(receipt.disposition, LearningDisposition.POSITIVE)
        self.assertTrue(receipt.learned)
        self.assertEqual(self.procedural._experience_buffer[-1]["success"], True)
        self.assertEqual(self.reward.total_rewards, 1)
        self.assertEqual(self.self_model.total_experiences, 1)
        self.assertEqual(len(receipt.memory_ids), 1)
        self.assertEqual(
            self.drive_events,
            [("activation-sentinel", "", True, 17)],
        )

        row = self.memory.conn.execute(
            "SELECT type, source, content FROM memories WHERE id = ?",
            (receipt.memory_ids[0],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "episodic")
        self.assertEqual(row["source"], "verified_learning_feedback")
        self.assertIn("已验证成功", row["content"])

    def test_unknown_and_simulated_success_are_quarantined(self):
        for index, quality in enumerate(("unknown", "simulated"), start=1):
            receipt = self.apply(
                verified(f"outcome-{index}", quality=quality, summary="工具声称完成")
            )
            self.assertEqual(receipt.disposition, LearningDisposition.QUARANTINED)
            self.assertFalse(receipt.learned)

        self.assertEqual(len(self.procedural._experience_buffer), 0)
        self.assertEqual(self.reward.total_rewards, 0)
        self.assertEqual(self.self_model.total_experiences, 0)
        self.assertEqual(self.memory.count(), 0)
        self.assertEqual(self.drive_events, [])

    def test_verified_or_explicit_failed_result_only_weakens(self):
        receipt = self.apply(
            verified(
                "outcome-failure",
                success=False,
                quality="failed",
                status="failed",
                summary="工具返回错误",
            )
        )

        self.assertEqual(receipt.disposition, LearningDisposition.NEGATIVE)
        self.assertTrue(receipt.learned)
        self.assertFalse(self.procedural._experience_buffer[-1]["success"])
        self.assertEqual(self.reward.total_rewards, 1)
        self.assertEqual(self.self_model.total_experiences, 1)
        self.assertIn("不作为成功范例", receipt.reflection)
        self.assertEqual(self.drive_events[-1][2], False)

    def test_legacy_structured_failure_without_quality_is_still_negative(self):
        receipt = self.apply(
            {
                "outcome_id": "legacy-failure",
                "episode_id": "episode-legacy-failure",
                "task_id": "task-legacy-failure",
                "status": "failed",
                "success": False,
                "summary": "旧结构化结果只给出 success=false",
                "tool_name": "memory_search",
            }
        )

        self.assertEqual(receipt.disposition, LearningDisposition.NEGATIVE)
        self.assertTrue(receipt.learned)
        self.assertFalse(self.procedural._experience_buffer[-1]["success"])
        self.assertEqual(self.reward.total_rewards, 1)
        self.assertEqual(self.self_model.total_experiences, 1)

    def test_contradiction_and_non_terminal_result_are_quarantined(self):
        contradiction = self.apply(
            verified("outcome-contradiction", success=True, quality="failed")
        )
        pending = self.apply(
            verified("outcome-pending", status="awaiting_feedback")
        )
        self.assertEqual(contradiction.disposition, LearningDisposition.QUARANTINED)
        self.assertEqual(pending.disposition, LearningDisposition.QUARANTINED)
        self.assertEqual(self.reward.total_rewards, 0)
        self.assertEqual(len(self.procedural._experience_buffer), 0)

    def test_missing_terminal_status_and_bare_verified_flag_are_quarantined(self):
        missing_status = self.apply(verified("missing-status", status=""))
        bare_flag = self.apply(
            {
                "outcome_id": "bare-verified-flag",
                "status": "completed",
                "success": True,
                "verified": True,
                "summary": "仅自报已验证",
            }
        )

        self.assertEqual(missing_status.disposition, LearningDisposition.QUARANTINED)
        self.assertEqual(bare_flag.disposition, LearningDisposition.QUARANTINED)
        self.assertEqual(self.drive_events, [])

    def test_duplicate_is_idempotent_and_snapshot_restores_guard(self):
        first = self.apply(verified("outcome-once"))
        duplicate = self.apply(verified("outcome-once"))
        self.assertEqual(first.disposition, LearningDisposition.POSITIVE)
        self.assertEqual(duplicate.disposition, LearningDisposition.DUPLICATE)
        self.assertEqual(self.reward.total_rewards, 1)
        self.assertEqual(len(self.procedural._experience_buffer), 1)
        self.assertEqual(len(self.drive_events), 1)

        restored = VerifiedLearningFeedback.from_snapshot(
            json.loads(json.dumps(self.feedback.snapshot(), ensure_ascii=False))
        )
        duplicate_after_restart = restored.apply(verified("outcome-once"))
        self.assertEqual(duplicate_after_restart.disposition, LearningDisposition.DUPLICATE)

    def test_summary_is_bounded_and_credentials_are_redacted(self):
        secret = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        receipt = self.apply(verified("outcome-secret", summary=secret))
        self.assertEqual(receipt.disposition, LearningDisposition.POSITIVE)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", self.procedural._experience_buffer[-1]["result"])
        row = self.memory.conn.execute(
            "SELECT content FROM memories WHERE id = ?", (receipt.memory_ids[0],)
        ).fetchone()
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", row["content"])
        self.assertIn("[REDACTED]", row["content"])

    def test_episode_record_is_accepted_without_importing_execution_module(self):
        record = EpisodeRecord(
            id="episode-record-1",
            trigger="task",
            goal_id="goal-1",
            goal="保存验证后的结果",
            task_id="task-1",
            status=EpisodeStatus.COMPLETED,
            success=True,
            result_quality="verified",
            tool_name="memory_search",
            result_summary="结果结构符合预期",
        )
        receipt = self.apply(record)
        self.assertEqual(receipt.disposition, LearningDisposition.POSITIVE)
        self.assertEqual(receipt.outcome_id, "episode-record-1")

    def test_component_failure_does_not_break_other_sinks(self):
        class BrokenProcedural:
            def record_experience(self, *args, **kwargs):
                raise RuntimeError("intentional test failure")

        receipt = self.feedback.apply(
            verified("outcome-component-error"),
            procedural_memory=BrokenProcedural(),
            reward_system=self.reward,
            self_model=self.self_model,
            memory_store=self.memory,
        )
        self.assertEqual(receipt.disposition, LearningDisposition.POSITIVE)
        self.assertTrue(receipt.learned)
        self.assertTrue(any("procedural_memory" in item for item in receipt.errors))
        self.assertEqual(self.reward.total_rewards, 1)
        self.assertEqual(self.self_model.total_experiences, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
