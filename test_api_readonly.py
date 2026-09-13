"""Read-only API contract tests for the V13 observability endpoints."""

from __future__ import annotations

import asyncio
import copy
import json
import types
import unittest
from unittest.mock import patch

from agent.tool_registry import ToolRegistry
from agent_bridge import AgentBridge
from brain.core import Brain
from brain.goal_system import Goal, GoalSystem
from brain.task_execution import TaskExecutionLedger
from brain.task_scheduler import LongTermTaskScheduler
from version import PRODUCT_VERSION, PRODUCT_VERSION_LABEL

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

    def test_api_metadata_uses_canonical_product_version(self):
        self.assertEqual(PRODUCT_VERSION, "0.1.0")
        self.assertEqual(PRODUCT_VERSION_LABEL, "v0.1")
        self.assertEqual(api_main.app.version, PRODUCT_VERSION)
        self.assertEqual(
            api_main.app.title,
            f"Brain Memory {PRODUCT_VERSION_LABEL}",
        )

    def test_health_reports_canonical_product_version(self):
        class _Memory:
            def count(self):
                return 0

            def get_identity_memories(self, _limit):
                return []

        async def _state():
            return {"total_ticks": 0, "uptime_seconds": 0.0}

        stem = types.SimpleNamespace(
            _task=None,
            state=types.SimpleNamespace(
                session_manager=types.SimpleNamespace(
                    get_session_count=lambda: 0,
                )
            ),
            task_scheduler=None,
            sleep_state="awake",
            boundary=None,
            task_execution=None,
            learning_feedback=None,
            drive_engine=None,
        )
        fake_brain = types.SimpleNamespace(
            get_state=_state,
            is_awake=False,
            memory_store=_Memory(),
            brain_stem=stem,
        )
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            result = self._call(api_main.health())
        finally:
            api_main._brain = previous_brain

        self.assertEqual(result["version"], PRODUCT_VERSION)
        self.assertEqual(result["product_version"], PRODUCT_VERSION)
        self.assertEqual(result["version_label"], PRODUCT_VERSION_LABEL)

    def test_health_redacts_legacy_state_and_bridge_diagnostics(self):
        class _Memory:
            def count(self):
                return 0

            def get_identity_memories(self, _limit):
                return []

        async def _state():
            return {
                "total_ticks": 1,
                "uptime_seconds": 1.0,
                "last_error": r"failed at C:\Users\secret\file.py",
                "last_loop_error": "https://example.invalid/private?token=secret",
                "life": {"db_path": r"C:\private\life.sqlite", "status": "alive"},
                "autonomy": {
                    "source_path": "/tmp/private/autonomy.json",
                    "enabled": True,
                    "raw_ledger": [{"event": "private"}],
                    "proofs": [{"signature": "private"}],
                    "unified_diff": "secret patch",
                },
            }

        stem = types.SimpleNamespace(
            _task=None,
            state=types.SimpleNamespace(
                session_manager=types.SimpleNamespace(get_session_count=lambda: 0),
            ),
            task_scheduler=None,
            sleep_state="awake",
            boundary=None,
            task_execution=None,
            learning_feedback=None,
            drive_engine=None,
        )
        bridge = types.SimpleNamespace(
            snapshot=lambda: {
                "enabled": True,
                "last_feed_error": r"C:\Users\secret\bridge.log",
                "workspace_root": r"C:\private\workspace",
            }
        )
        fake_brain = types.SimpleNamespace(
            get_state=_state,
            is_awake=False,
            memory_store=_Memory(),
            brain_stem=stem,
        )
        previous_brain = api_main._brain
        previous_bridge = api_main._bridge
        api_main._brain = fake_brain
        api_main._bridge = bridge
        try:
            result = self._call(api_main.health())
        finally:
            api_main._brain = previous_brain
            api_main._bridge = previous_bridge

        encoded = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            r"C:\Users\secret\file.py",
            r"C:\private\life.sqlite",
            "/tmp/private/autonomy.json",
            "https://example.invalid/private",
            "workspace_root",
            "db_path",
            "source_path",
            "raw_ledger",
            "proofs",
            "unified_diff",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(result["last_error"], "<redacted>")
        self.assertEqual(result["last_loop_error"], "<redacted>")
        self.assertEqual(result["life"]["status"], "alive")
        self.assertEqual(result["bridge"]["last_feed_error"], "<redacted>")

    def test_state_route_redacts_legacy_capability_fields(self):
        async def _state():
            return {
                "current_emotion": "curious",
                "last_intent": {
                    "type": "call_tool",
                    "tool_args": {
                        "path": r"candidate\brain\drive_engine.py",
                    },
                },
                "last_error": "DEEPSEEK_API_KEY=plain-looking-secret",
                "life": {
                    "status": "active",
                    "candidate_path": r"C:\private\candidate",
                    "ledger": [{"event": "private"}],
                },
                "autonomy": {"events": [{"raw": "private"}]},
                r"C:\private\path-as-key": "must not survive",
                "safe": "ordinary state",
            }

        stem = types.SimpleNamespace(
            sleep_state="awake",
        )
        fake_brain = types.SimpleNamespace(
            get_state=_state,
            brain_stem=stem,
        )
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            result = self._call(api_main.get_state())
        finally:
            api_main._brain = previous_brain

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["current_emotion"], "curious")
        self.assertEqual(result["safe"], "ordinary state")
        self.assertEqual(result["last_intent"]["tool_args"]["path"], "<redacted>")
        self.assertEqual(result["last_error"], "<redacted>")
        self.assertNotIn(r"C:\private\candidate", encoded)
        self.assertNotIn("path-as-key", encoded)
        self.assertNotIn("candidate_path", encoded)
        self.assertNotIn('"ledger"', encoded)
        self.assertNotIn('"events"', encoded)

    def test_input_response_and_websocket_broadcast_use_public_projection(self):
        projected = []

        class _Brain:
            brain_stem = types.SimpleNamespace()

            async def process_input(self, **_kwargs):
                return {
                    "safe": "ok",
                    "candidate": r"candidate\brain\drive_engine.py",
                    "diagnostic": "DEEPSEEK_API_KEY=plain-looking-secret",
                    "response": {
                        "text": r"C:\private\response.txt",
                        "safe": "kept",
                    },
                }

            async def broadcast_state(self, *, projector=None):
                self.assert_projector = projector
                projected.append(projector({
                    "safe": "ok",
                    "candidate": r"candidate\brain\drive_engine.py",
                }))

        fake_brain = _Brain()
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            request = api_main.InputRequest(text="hello", source="external")
            result = self._call(api_main.post_input(request))
        finally:
            api_main._brain = previous_brain

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["safe"], "ok")
        self.assertNotIn("drive_engine.py", encoded)
        self.assertNotIn("plain-looking-secret", encoded)
        self.assertEqual(result["response"]["text"], "<redacted>")
        self.assertEqual(result["response"]["safe"], "kept")
        self.assertEqual(projected, [{"safe": "ok", "candidate": "<redacted>"}])

    def test_brain_broadcast_state_never_falls_back_to_raw_snapshot(self):
        """The core WebSocket helper must fail closed even with an identity projector."""
        class _Client:
            def __init__(self):
                self.messages = []

            async def send_text(self, payload):
                self.messages.append(payload)

        class _ProbeBrain(Brain):
            def __init__(self, client):
                self._ws_clients = {client}
                self.brain_stem = types.SimpleNamespace(
                    _ensure_loop_primitives=lambda: None,
                )

            def _ensure_loop_primitives(self):
                return None

            async def get_state(self):
                return {
                    "safe": "ok",
                    "last_intent": {
                        "tool_args": {
                            "path": r"candidate\brain\drive_engine.py",
                        },
                    },
                    "diagnostic": "DEEPSEEK_API_KEY=plain-looking-secret",
                    "events": [{"private": "ledger material"}],
                }

        client = _Client()
        brain = _ProbeBrain(client)
        self._call(brain.broadcast_state(projector=lambda value: value))

        self.assertEqual(len(client.messages), 1)
        payload = json.loads(client.messages[0])
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["data"]["safe"], "ok")
        self.assertEqual(payload["data"]["last_intent"]["tool_args"]["path"], "<redacted>")
        self.assertNotIn("drive_engine.py", encoded)
        self.assertNotIn("plain-looking-secret", encoded)
        self.assertNotIn('"events"', encoded)

    def test_public_projection_redacts_dynamic_keys_auth_headers_and_cycles(self):
        from brain.public_projection import sanitize_public_projection

        cyclic = {}
        cyclic["self"] = cyclic
        value = {
            r"C:\private\dynamic-key": "should disappear",
            "path": "candidate/run-123",
            "authorization": "Bearer super-secret-token",
            "auth_header": "Token opaque-secret",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "aws": "AKIAIOSFODNN7EXAMPLE",
            "DEEPSEEK_API_KEY": "plain-secret",
            "AWS_ACCESS_KEY_ID": "plain-secret",
            "diagnostic": "DEEPSEEK_API_KEY=plain-looking-secret",
            "diagnostic_aws": "aws_secret_access_key=abc123",
            "diagnostic_candidate": "CANDIDATE_ROOT=candidate/run-123",
            "candidatePath": r"candidate\private\file.py",
            "workspaceRoot": r"C:\private\workspace",
            "cyclic": cyclic,
        }

        result = sanitize_public_projection(value)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("dynamic-key", encoded)
        self.assertEqual(result["path"], "<redacted>")
        self.assertEqual(result["authorization"], "<redacted>")
        self.assertEqual(result["auth_header"], "<redacted>")
        self.assertEqual(result["jwt"], "<redacted>")
        self.assertEqual(result["aws"], "<redacted>")
        self.assertNotIn("DEEPSEEK_API_KEY", result)
        self.assertNotIn("AWS_ACCESS_KEY_ID", result)
        self.assertEqual(result["diagnostic"], "<redacted>")
        self.assertEqual(result["diagnostic_aws"], "<redacted>")
        self.assertEqual(result["diagnostic_candidate"], "<redacted>")
        self.assertNotIn("candidatePath", result)
        self.assertNotIn("workspaceRoot", result)
        self.assertEqual(result["cyclic"], {})

    def test_websocket_origin_defaults_to_local_or_explicit_allowlist(self):
        local = types.SimpleNamespace(headers={"origin": "http://127.0.0.1:8001"})
        absent = types.SimpleNamespace(headers={})
        hostile = types.SimpleNamespace(headers={"origin": "https://evil.example"})
        self.assertTrue(api_main._websocket_origin_allowed(local))
        self.assertTrue(api_main._websocket_origin_allowed(absent))
        self.assertFalse(api_main._websocket_origin_allowed(hostile))
        with patch.object(api_main, "_cors_origins", ["https://operator.example/"]):
            explicit = types.SimpleNamespace(
                headers={"origin": "https://operator.example"}
            )
            self.assertTrue(api_main._websocket_origin_allowed(explicit))

    def test_continuity_route_is_read_only_and_strips_capability_material(self):
        calls = []

        class _Stem:
            def continuity_readiness_snapshot(self):
                calls.append("read")
                return {
                    "schema_version": 1,
                    "read_only": True,
                    "ready": False,
                    "status": "needs_host",
                    "workspace_root": r"C:\private\workspace",
                    "nested": {
                        "candidate_path": r"C:\private\candidate",
                        "deepseek_api_key": "not-a-secret-shaped-value",
                        "safe": "ok",
                        "diagnostic": r"failed at C:\private\candidate\file.py",
                        "posix_diagnostic": "/workspace/private/candidate.py",
                        "remote_diagnostic": "https://example.invalid/private?token=redact-me",
                    },
                }

        fake_brain = types.SimpleNamespace(brain_stem=_Stem())
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            result = self._call(api_main.get_continuity())
        finally:
            api_main._brain = previous_brain

        self.assertEqual(calls, ["read"])
        self.assertTrue(result["read_only"])
        self.assertNotIn("workspace_root", result)
        self.assertNotIn("candidate_path", result.get("nested", {}))
        self.assertNotIn("deepseek_api_key", result.get("nested", {}))
        self.assertEqual(result["nested"]["safe"], "ok")
        self.assertEqual(result["nested"]["diagnostic"], "<redacted>")
        self.assertEqual(result["nested"]["posix_diagnostic"], "<redacted>")
        self.assertEqual(result["nested"]["remote_diagnostic"], "<redacted>")

    def test_agent_bridge_snapshot_does_not_publish_workspace_root(self):
        bridge = AgentBridge(
            None,
            ToolRegistry(),
            workspace_root=r"C:\private\workspace",
        )
        self.assertNotIn("workspace_root", bridge.snapshot())

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

    def test_health_goals_and_v11_get_routes_do_not_sync_scheduler(self):
        """All documented GET observability routes must remain side-effect free."""
        goal_before = copy.deepcopy(self.goal.to_dict())
        scheduler_before = copy.deepcopy(
            {
                key: value
                for key, value in self.scheduler.__dict__.items()
                if key != "sync_calls"
            }
        )

        memory = types.SimpleNamespace(
            count=lambda: 0,
            get_identity_memories=lambda _limit: [],
        )

        async def _state():
            return {"total_ticks": 42, "uptime_seconds": 1.0}

        stem = types.SimpleNamespace(
            _task=None,
            state=types.SimpleNamespace(
                total_ticks=42,
                session_manager=types.SimpleNamespace(get_session_count=lambda: 0),
            ),
            goal_system=self.goals,
            task_scheduler=self.scheduler,
            task_execution=None,
            learning_feedback=None,
            drive_engine=None,
            boundary=None,
            sleep_state="awake",
        )
        fake_brain = types.SimpleNamespace(
            brain_stem=stem,
            get_state=_state,
            is_awake=False,
            memory_store=memory,
        )
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            health = self._call(api_main.health())
            goals = self._call(api_main.get_goals())
            tasks = self._call(api_main.get_long_term_tasks())
        finally:
            api_main._brain = previous_brain

        self.assertIn("tasks", health)
        self.assertIn("task_scheduler", goals)
        self.assertEqual(tasks["last_tick"], 7)
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

    def test_working_memory_reports_configured_capacity(self):
        wm = types.SimpleNamespace(
            get_state_snapshot=lambda: [],
            get_context=lambda: "",
        )
        fake_brain = types.SimpleNamespace(
            brain_stem=types.SimpleNamespace(
                working_memory=wm,
                state=types.SimpleNamespace(activation=object()),
            )
        )
        previous_brain = api_main._brain
        api_main._brain = fake_brain
        try:
            with patch.object(api_main, "WORKING_MEMORY_CAPACITY", 11):
                result = self._call(api_main.get_working_memory_state())
        finally:
            api_main._brain = previous_brain

        self.assertEqual(result["capacity"], 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
