"""Process-level V13 smoke checks.

These tests intentionally use a temporary SQLite database and a local
read-only tool stub.  They exercise the boundaries that unit tests cannot
prove on their own: API startup against an isolated store, restart recovery
without replay, and a tool failure that must close/cancel one exact causal
lane while leaving no action available for replay.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent.tool_registry import ToolDef, ToolRegistry
from agent_bridge import AgentBridge
from brain.autonomy import EpisodeStatus
from brain.goal_system import Goal, GoalStatus
from brain.intent import Intent, IntentType
from brain.task_execution import ActionStatus, OutcomeQuality, PlanStatus, TaskExecutionLedger
from brain.core import Brain
from storage.database import init_db


ROOT = Path(__file__).resolve().parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str, timeout: float = 2.0):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Connection": "close"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), json.loads(response.read().decode("utf-8"))


def _stop_process_tree(process: subprocess.Popen) -> None:
    """Stop uvicorn and the Windows venv launcher child, if any."""
    if process.poll() is None:
        if os.name == "nt":
            # ``.venv\\Scripts\\python.exe`` can be a launcher whose real
            # interpreter is a child process.  Popen.terminate() only stops
            # the launcher and leaves SQLite handles open, so terminate the
            # exact process tree before waiting for cleanup.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


class V13ApiProcessSmokeTests(unittest.TestCase):
    def test_api_starts_on_isolated_database_and_exposes_read_only_metrics(self):
        with tempfile.TemporaryDirectory(prefix="brain-v13-api-smoke-") as temp_dir:
            db_path = str(Path(temp_dir) / "isolated.sqlite")
            port = _free_port()
            env = os.environ.copy()
            env.update(
                {
                    "BRAIN_MEMORY_DB_PATH": db_path,
                    "BRAIN_MEMORY_OFFLINE": "1",
                    "BRAIN_MEMORY_HOST": "127.0.0.1",
                    "BRAIN_MEMORY_PORT": str(port),
                    "BRAIN_MEMORY_AGENT_BRIDGE_ENABLED": "0",
                    "BRAIN_MEMORY_AUTONOMY_ENABLED": "0",
                    "BRAIN_MEMORY_COGNITIVE_TIMEOUT_SEC": "1",
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            base = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 15.0
                last_error = ""
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(
                            f"uvicorn exited before readiness (code={process.returncode})"
                        )
                    try:
                        status, health = _get_json(f"{base}/api/v4/health")
                        if status == 200 and health.get("status") in {
                            "awake",
                            "degraded",
                        }:
                            break
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        last_error = str(exc)
                        time.sleep(0.1)
                else:
                    self.fail(f"isolated API did not become ready: {last_error}")

                status, listing = _get_json(f"{base}/api/v13/tasks")
                self.assertEqual(status, 200)
                self.assertTrue(listing["enabled"])
                self.assertIn("execution", listing)

                status, metrics = _get_json(f"{base}/api/v13/metrics")
                self.assertEqual(status, 200)
                self.assertIn("execution", metrics)
                self.assertIn("learning", metrics)
                self.assertIn("drive_feedback", metrics)
                self.assertTrue(metrics["execution"]["enabled"])

                try:
                    _get_json(f"{base}/api/v13/tasks/does-not-exist")
                except urllib.error.HTTPError as missing:
                    # HTTPError owns the response socket; close it explicitly
                    # before terminating uvicorn so Windows cannot retain a
                    # live request handle while the temporary SQLite store
                    # is being removed.
                    try:
                        self.assertEqual(missing.code, 404)
                    finally:
                        missing.close()
                else:
                    self.fail("missing V13 plan unexpectedly returned 200")
            finally:
                _stop_process_tree(process)


class V13RestartAndFaultSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_cancels_inflight_action_and_preserves_isolation(self):
        with tempfile.TemporaryDirectory(prefix="brain-v13-restart-smoke-") as temp_dir:
            db_path = str(Path(temp_dir) / "restart.sqlite")
            init_db(db_path)
            first = Brain(db_path)
            second = None
            try:
                stem = first.brain_stem
                goal = Goal(
                    id="smoke-restart-goal",
                    drive="growth",
                    description="重启恢复 smoke",
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
                            "id": "restart-step",
                            "description": "等待桥接结果",
                            "tool_name": "memory_search",
                        }
                    ],
                    current_tick=1,
                )
                self.assertIsNotNone(plan)
                stem.task_scheduler.running_goal_id = goal.id
                step = stem.task_execution.next_step(plan_id=plan.id, current_tick=2)
                action = stem.task_execution.record_action(
                    plan.id,
                    step.id,
                    episode_id="smoke-restart-episode",
                    action_id="smoke-restart-action",
                )
                self.assertIsNotNone(action)
                await stem._snapshot_state()
                await first.sleep()
                first = None

                second = Brain(db_path)
                await second.wake_up()
                restored_plan = second.brain_stem.task_execution.get_plan(plan.id)
                self.assertIsNotNone(restored_plan)
                self.assertEqual(restored_plan.status, PlanStatus.PAUSED)
                restored_action = second.brain_stem.task_execution.actions[action.id]
                self.assertEqual(restored_action.status, ActionStatus.CANCELLED)
                restored_goal = second.brain_stem.goal_system.get_by_id(goal.id)
                self.assertIsNotNone(restored_goal)
                self.assertEqual(restored_goal.status, GoalStatus.PAUSED)
                self.assertIsNone(second.brain_stem.task_scheduler.running_goal_id)
                self.assertIsNone(second.brain_stem.autonomy.active)
            finally:
                if first is not None:
                    await first.sleep()
                if second is not None:
                    await second.sleep()

    async def test_faulty_read_only_tool_is_failed_without_replay(self):
        # Keep the fault path on the same isolated SQLite boundary as the API
        # and restart checks.  No project/default database is touched.
        with tempfile.TemporaryDirectory(prefix="brain-v13-fault-smoke-") as temp_dir:
            db_path = str(Path(temp_dir) / "fault.sqlite")
            init_db(db_path)
            brain = Brain(db_path)
            stem = brain.brain_stem
            try:
                goal = Goal(
                    id="smoke-fault-goal",
                    drive="coherence",
                    description="故障恢复 smoke",
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
                            "id": "fault-step",
                            "description": "调用会失败的只读工具",
                            "tool_name": "failing_read",
                            "criteria": {"required_fields": ["results"]},
                        }
                    ],
                    current_tick=1,
                )
                self.assertIsNotNone(plan)
                episode = stem.autonomy.begin(
                    goal.id,
                    goal.description,
                    drive=goal.drive,
                    task_id=plan.id,
                    tick=1,
                )
                step = stem.task_execution.next_step(plan_id=plan.id, current_tick=1)
                action = stem.task_execution.record_action(
                    plan.id,
                    step.id,
                    episode_id=episode.id,
                    action_id="smoke-fault-action",
                )
                self.assertIsNotNone(action)
                intent_id = "smoke-fault-intent"
                self.assertTrue(
                    stem.autonomy.plan(
                        episode.id,
                        "call_tool",
                        "failing_read",
                        tick=1,
                        intent_id=intent_id,
                        plan_id=plan.id,
                        step_id=step.id,
                        action_id=action.id,
                    )
                )

                calls = 0

                async def failing_read(_args, _context):
                    nonlocal calls
                    calls += 1
                    raise RuntimeError("intentional smoke failure")

                registry = ToolRegistry()
                registry.register(
                    ToolDef(
                        name="failing_read",
                        description="local failing read-only stub",
                        schema={},
                        call=failing_read,
                        is_read_only=True,
                    )
                )
                bridge = AgentBridge(stem, registry)
                intent = Intent(
                    type=IntentType.CALL_TOOL,
                    tool_name="failing_read",
                    tool_args={},
                    episode_id=episode.id,
                    goal_id=goal.id,
                    intent_id=intent_id,
                    plan_id=plan.id,
                    step_id=step.id,
                    action_id=action.id,
                )
                await bridge._handle_call_tool(intent)
                self.assertEqual(calls, 1)
                await stem._tick()

                self.assertIsNone(stem.autonomy.active)
                self.assertEqual(stem.autonomy.last_closed.status, EpisodeStatus.FAILED)
                self.assertEqual(action.status, ActionStatus.EVALUATED)
                outcome = stem.task_execution.outcomes[action.outcome_id]
                self.assertEqual(outcome.status, OutcomeQuality.FAILED)
                self.assertNotEqual(plan.status, PlanStatus.COMPLETED)
                # A second delivery of the same intent is stale and must not
                # invoke the tool again or create another result.
                await bridge._handle_call_tool(intent)
                self.assertEqual(calls, 1)
                self.assertEqual(len(stem.task_execution.outcomes), 1)
            finally:
                # The test does not start the heartbeat, but Brain owns lazy
                # SQLite connections; close them explicitly before the temp
                # directory is removed (especially important on Windows).
                brain.state_store.close()
                brain.memory_store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
