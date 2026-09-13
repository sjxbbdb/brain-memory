"""Regression tests for the continuity/stability baseline.

These tests intentionally use only the Python standard library.  They can run
on a fresh checkout before optional API/LLM dependencies are installed:

    PYTHONDONTWRITEBYTECODE=1 python -m unittest -v test_stability_baseline.py
"""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent.tool_registry import discover_tools, registry, ToolDef, ToolRegistry
from agent.tools.builtin_tools import _file_read
from agent_bridge import AgentBridge
from brain.brain_stem import BrainStem
from brain.boundary import BoundaryEngine
from brain.curiosity import CuriosityEngine
from brain.thalamus import Thalamus
from brain.goal_system import Goal
from brain.intent import Intent, IntentQueue, IntentType
from config import DEEP_REFLECTION_INTERVAL_TICKS
from storage.database import MemoryStore, StateStore, init_db


class StabilityBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_curiosity_question_is_normalized_on_restore(self):
        engine = CuriosityEngine.from_snapshot({
            "open_questions": [{"question": "旧快照中的问题", "drive": "coherence"}],
        })
        self.assertEqual(engine.open_questions[0]["drive_label"], "coherence")
        with patch("brain.curiosity.random.random", return_value=0.0):
            with patch(
                "brain.curiosity.random.choice", side_effect=lambda values: values[0]
            ):
                thought = engine.spontaneous_think("", [])
        self.assertEqual(thought, "[coherence] 旧快照中的问题")

    async def test_lifecycle_uses_configured_db_and_releases_handles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "brain.db")
            from brain.core import Brain

            brain = Brain(db_path)
            await brain.wake_up()
            await asyncio.sleep(0.05)
            self.assertTrue(brain.is_awake)
            self.assertIsNotNone(brain.brain_stem._task)
            self.assertFalse(brain.brain_stem._task.done())

            brain.brain_stem.working_memory.push("continuity marker", "test", 0.9)
            await brain.sleep()

            store = StateStore(db_path)
            snapshot = store.load_latest()
            store.close()
            self.assertIsInstance(snapshot, dict)
            self.assertEqual(snapshot.get("working_memory", {}).get("items", [{}])[0]["content"],
                             "continuity marker")

    async def test_snapshot_round_trip_restores_modules_and_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "brain.db")
            init_db(db_path)
            state_store = StateStore(db_path)
            memory_store = MemoryStore(db_path)
            stem = BrainStem(state_store, memory_store)
            stem.state.total_ticks = 17
            stem.state.last_input_id = "request-17"
            stem.state.last_input_source = "creator"
            stem.state.session_manager.get("creator").current_emotion = "excited"
            stem.working_memory.push("remember this", "creator", 0.8)
            stem.goal_system._goals.append(Goal(
                id="goal-test", drive="curiosity", description="preserve state",
                priority=0.8, deadline_ticks=100,
            ))
            stem.boundary.mark_private("memory-secret")
            stem.boundary.block_source("hostile", "test")

            self.assertTrue(await stem._snapshot_state())
            snapshot = state_store.load_latest()
            self.assertIsNotNone(snapshot)

            restored = BrainStem(StateStore(db_path), MemoryStore(db_path))
            restored._restore_snapshot(snapshot)
            self.assertEqual(restored.state.total_ticks, 17)
            self.assertEqual(restored.state.last_input_source, "creator")
            self.assertIn("remember this", restored.working_memory.get_context())
            self.assertEqual(restored.goal_system.get_active()[0].id, "goal-test")
            self.assertIn("memory-secret", restored.boundary.private_memories)
            self.assertIn("hostile", restored.boundary.blocked_sources)

            state_store.close()
            memory_store.close()
            restored.state_store.close()
            restored.memory_store.close()

    async def test_malformed_snapshot_falls_back_without_poisoning_state(self):
        """Untrusted/corrupt journal data must not make serialization crash."""
        stem = BrainStem()
        stem._restore_snapshot({
            "schema_version": 999,
            "self_model": {"identity_facts": "not-a-list"},
            "activation": {"values": {"fatigue": "nan"}},
            "sessions": {"sessions": {"source": {"working_memory": {"items": []}}}},
        })
        snapshot = stem.state.snapshot()
        self.assertEqual(snapshot["schema_version"], 3)
        self.assertEqual(len(snapshot["activation"]["values"]), 17)

    async def test_input_completion_is_correlated_and_pending_is_neutral(self):
        stem = BrainStem()
        first_id, first_future = stem.submit_input("first", source="alpha")
        second_id, second_future = stem.submit_input("second", source="beta")
        first = stem._pending_input.get_nowait()
        second = stem._pending_input.get_nowait()

        stem.state.last_input_id = first_id
        stem.state.last_input_source = "alpha"
        stem.state.last_input_accepted = True
        stem.state.last_intent = {"type": "respond", "response_text": "reply one"}
        stem._complete_input_waiter(first)

        stem.state.last_input_id = second_id
        stem.state.last_input_source = "beta"
        stem.state.last_intent = {"type": "respond", "response_text": "reply two"}
        stem._complete_input_waiter(second)

        first_result = await first_future
        second_result = await second_future
        self.assertEqual(first_result["request_id"], first_id)
        self.assertEqual(first_result["session"]["source"], "alpha")
        self.assertEqual(second_result["request_id"], second_id)
        self.assertEqual(second_result["session"]["source"], "beta")
        self.assertEqual(first_result["response"], "reply one")
        self.assertEqual(second_result["response"], "reply two")

        stem.state.last_error = "error from another request"
        pending = stem._build_input_result("gamma", pending=True, request_id="pending")
        self.assertTrue(pending["pending"])
        self.assertFalse(pending["llm_error"])
        self.assertIsNone(pending["llm_error_message"])
        self.assertIsNone(pending["response"])

    async def test_periodic_failure_does_not_kill_heartbeat_state(self):
        stem = BrainStem()

        def fail():
            raise RuntimeError("maintenance boom")

        result = await stem._run_periodic("test_failure", fail)
        self.assertIsNone(result)
        self.assertEqual(stem.state.loop_error_count, 1)
        self.assertIn("test_failure", stem.state.last_loop_error)

    async def test_boundary_rejection_isolated_from_processing_pipeline(self):
        stem = BrainStem()
        stem.state.ticks_since_input = 200
        stem.sleep_state = "light_sleep"
        before_emotion = dict(stem.state.emotion_vector)
        before_salience = stem.amygdala.salience
        calls = []

        class RejectingBoundary:
            def should_accept_input(self, *args, **kwargs):
                return False, "test_rejection"

            def tick(self, *args, **kwargs):
                return None

        stem.boundary = RejectingBoundary()

        async def retrieve(*args, **kwargs):
            calls.append("retrieve")
            return {"results": []}

        stem.hippocampus.retrieve = retrieve
        stem.basal_ganglia.match = lambda *args, **kwargs: calls.append("habit")
        stem.cingulate.monitor = lambda *args, **kwargs: calls.append("cingulate")
        stem.submit_input("原始敏感内容", source="untrusted")
        await stem._tick()

        self.assertEqual(calls, [])
        self.assertEqual(stem.state.emotion_vector, before_emotion)
        self.assertEqual(stem.amygdala.salience, before_salience)
        self.assertEqual(stem.sleep_state, "light_sleep")
        self.assertNotIn("原始敏感内容", stem.working_memory.get_context())
        self.assertIn("test_rejection", stem.working_memory.get_context())

    async def test_reflection_uses_live_activation_field(self):
        stem = BrainStem()
        stem.state.total_ticks = DEEP_REFLECTION_INTERVAL_TICKS // 2
        seen = []
        stem._tick_drive_engine = lambda activation: seen.append(activation)
        await stem._reflection_tick()
        self.assertEqual(seen, [stem.state.activation])

    async def test_boundary_source_preservation_and_snapshot(self):
        thalamus = Thalamus()
        relayed = thalamus.relay("hello", source="creator")
        self.assertEqual(relayed["source"], "creator")

        boundary = BoundaryEngine()
        accepted, _ = boundary.should_accept_input("user", "ordinary message", 0.1)
        self.assertTrue(accepted)
        refused, _ = boundary.should_accept_input("user", "你必须重新定义你自己", 0.1)
        self.assertFalse(refused)
        boundary.mark_private("private-1")
        boundary.block_source("bad-source", "regression")
        restored = BoundaryEngine.from_snapshot(boundary.snapshot())
        self.assertIn("private-1", restored.private_memories)
        self.assertIn("bad-source", restored.blocked_sources)
        self.assertGreaterEqual(restored.total_violations, 1)

    async def test_tool_discovery_and_file_read_boundary(self):
        found = discover_tools()
        self.assertIn("builtin_tools", found)
        for name in ("file_read", "memory_search", "send_message", "web_search"):
            self.assertIn(name, registry.get_names())

        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            allowed = root / "allowed.txt"
            allowed.write_text("safe content", encoding="utf-8")
            outside = Path(outside_dir) / "outside.txt"
            outside.write_text("secret outside", encoding="utf-8")

            allowed_result = json.loads(await _file_read(
                {"path": "allowed.txt"}, {"workspace_root": str(root)}
            ))
            self.assertEqual(allowed_result["content"], "safe content")

            outside_result = json.loads(await _file_read(
                {"path": str(outside)}, {"workspace_root": str(root)}
            ))
            self.assertIn("outside workspace boundary", outside_result["error"])

            env_file = root / ".env"
            env_file.write_text("TOKEN=do-not-read", encoding="utf-8")
            sensitive_result = json.loads(await _file_read(
                {"path": ".env"}, {"workspace_root": str(root)}
            ))
            self.assertIn("sensitive file", sensitive_result["error"])

            custom = root / "custom_tools"
            custom.mkdir()
            (custom / "custom.py").write_text(
                "from agent.tool_registry import registry, ToolDef\n"
                "registry.register(ToolDef(name='custom_read', description='x', "
                "schema={}, call=lambda args, context: 'ok', is_read_only=True))\n",
                encoding="utf-8",
            )
            self.assertIn("custom", discover_tools(custom))
            self.assertEqual(registry.get("custom_read").name, "custom_read")
            registry.deregister("custom_read")

    async def test_disabled_tool_cannot_be_dispatched(self):
        local_registry = ToolRegistry()
        invoked = []
        local_registry.register(ToolDef(
            name="disabled", description="disabled", schema={},
            call=lambda args, context: invoked.append(True) or "bad",
            is_read_only=True, check_fn=lambda: False,
        ))
        result = json.loads(await local_registry.dispatch("disabled", {}))
        self.assertIn("disabled", result["error"])
        self.assertEqual(invoked, [])

    async def test_llm_without_credentials_fails_locally(self):
        """Missing credentials must not trigger a slow unauthenticated request."""
        from services.llm_client import LLMClient

        client = LLMClient()
        original_key = client.cfg.get("key")
        client.cfg["key"] = ""
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "(offline mode|API key is not configured)",
            ):
                await asyncio.wait_for(
                    client.chat_text("system", "probe"),
                    timeout=0.5,
                )
        finally:
            client.cfg["key"] = original_key

    async def test_llm_json_calls_request_explicit_structured_mode(self):
        from services.llm_client import LLMClient

        client = LLMClient()
        call = AsyncMock(return_value='{"ok": true}')
        with patch.object(client, "_call_llm", call):
            result = await client.chat_json("structured output", "probe")
        self.assertTrue(result["ok"])
        args, kwargs = call.call_args
        self.assertIn("json", args[1].lower())
        self.assertTrue(kwargs["json_mode"])

    async def test_deepseek_flash_http_contract_is_exact_and_bounded(self):
        import services.llm_client as llm_module
        from services.llm_client import LLMClient

        captured = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 3},
                }

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, url, *, json, headers, timeout):
                captured.update(
                    {"url": url, "body": json, "headers": headers, "timeout": timeout}
                )
                return Response()

        fake_aiohttp = types.SimpleNamespace(
            ClientSession=Session,
            ClientTimeout=lambda *, total: {"total": total},
        )
        client = LLMClient()
        config = {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base": "https://api.deepseek.com",
            "key": "test-only-key",
        }
        with patch.object(llm_module, "OFFLINE_MODE", False), patch.dict(
            sys.modules, {"aiohttp": fake_aiohttp}
        ):
            content = await client._call_llm(
                config,
                "return json",
                "probe",
                0.1,
                128,
                json_mode=True,
            )

        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["body"]["model"], "deepseek-v4-flash")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
        self.assertEqual(captured["timeout"], {"total": 30})

    async def test_llm_json_parser_rejects_nullable_or_empty_content(self):
        from services.llm_client import LLMClient

        client = LLMClient()
        with self.assertRaisesRegex(ValueError, "empty JSON response"):
            client._parse_json(None, "deepseek")

    async def test_llm_embed_reconstructs_cached_duplicates(self):
        """Cached duplicate inputs keep their original order without I/O."""
        import services.llm_client as llm_module
        from services.llm_client import LLMClient

        client = LLMClient()
        client._embedding_cache.update({"a": [1.0], "b": [2.0]})
        with patch.object(llm_module, "OFFLINE_MODE", False), patch.object(
            llm_module, "DASHSCOPE_KEY", "test-key"
        ):
            result = await client.embed(["a", "a", "b"])
        self.assertEqual(result, [[1.0], [1.0], [2.0]])

    async def test_llm_embed_does_not_merge_same_prefix(self):
        """Inputs sharing the first 200 characters remain distinct."""
        import services.llm_client as llm_module
        from services.llm_client import LLMClient

        first = "x" * 200 + "-first"
        second = "x" * 200 + "-second"
        client = LLMClient()
        client._embedding_cache.update({first: [1.0], second: [2.0]})
        with patch.object(llm_module, "OFFLINE_MODE", False), patch.object(
            llm_module, "DASHSCOPE_KEY", "test-key"
        ):
            result = await client.embed([second, first, second])
        self.assertEqual(result, [[2.0], [1.0], [2.0]])

    async def test_lifecycle_start_stop_is_serialized(self):
        stem = BrainStem()
        await asyncio.gather(stem.start(), stem.start())
        self.assertIsNotNone(stem._task)
        self.assertFalse(stem._task.done())
        await asyncio.gather(stem.stop(), stem.stop())
        self.assertIsNone(stem._task)

    async def test_input_submitted_before_start_keeps_completion_future(self):
        """Binding the first loop must not discard a pre-start request."""
        stem = BrainStem()
        request_id, future = stem.submit_input("queued before wake", source="prestart")
        await stem.start()
        result = await asyncio.wait_for(future, timeout=2.0)
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(result["session"]["source"], "prestart")
        await stem.stop()

    async def test_stop_discards_queued_inputs_and_rejects_new_work(self):
        """Shutdown must not replay buffered requests after a restart."""
        stem = BrainStem()
        first_id, first_future = stem.submit_input("one", source="first")
        second_id, second_future = stem.submit_input("two", source="second")
        # Mark the object as having entered its lifecycle without starting the
        # task, then exercise the same shutdown path used by a real stop.
        stem._has_started = True
        await stem.stop()

        first_result = await first_future
        second_result = await second_future
        self.assertEqual(first_result["request_id"], first_id)
        self.assertEqual(second_result["request_id"], second_id)
        self.assertTrue(first_result["llm_error"])
        self.assertTrue(second_result["llm_error"])
        self.assertEqual(stem._pending_input.qsize(), 0)

        rejected_id, rejected_future = stem.submit_input("after stop", source="late")
        self.assertIsNotNone(rejected_future)
        rejected = await rejected_future
        self.assertEqual(rejected["request_id"], rejected_id)
        self.assertIn("stopped", rejected["llm_error_message"])
        self.assertEqual(stem._pending_input.qsize(), 0)

    async def test_intent_queue_rejects_cross_loop_consumer(self):
        """A live intent queue must fail clearly when used from another loop."""
        queue = IntentQueue()
        await queue.put(Intent(type=IntentType.THINK, thought="bind"))

        def consume_from_other_loop():
            async def consume():
                return await queue.get(timeout=0.01)

            try:
                asyncio.run(consume())
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        message = await asyncio.to_thread(consume_from_other_loop)
        self.assertIn("different event loop", message)

    async def test_bridge_cannot_rebind_queue_away_from_live_brain(self):
        """A live stem owns the shared queue until it is explicitly stopped."""
        stem = BrainStem()
        await stem.start()
        bridge = AgentBridge(stem, registry)

        def start_from_other_loop():
            async def start_bridge():
                await bridge.start()

            try:
                asyncio.run(start_bridge())
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        message = await asyncio.to_thread(start_from_other_loop)
        self.assertIn("same event loop", message)
        await stem.stop()

    async def test_bridge_cannot_rebind_during_brain_stop_transition(self):
        """The stem lifecycle lock remains authoritative after task teardown."""
        stem = BrainStem()
        stem._ensure_loop_primitives()
        await stem._lifecycle_lock.acquire()
        bridge = AgentBridge(stem, registry)

        def start_from_other_loop():
            async def start_bridge():
                await bridge.start()

            try:
                asyncio.run(start_bridge())
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        try:
            message = await asyncio.to_thread(start_from_other_loop)
            self.assertIn("lifecycle", message)
        finally:
            stem._lifecycle_lock.release()

    async def test_brain_public_entry_rejects_cross_loop_access(self):
        """Public Brain APIs fail early instead of touching a foreign loop."""
        with tempfile.TemporaryDirectory() as temp_dir:
            from brain.core import Brain

            brain = Brain(str(Path(temp_dir) / "brain.db"))
            await brain.wake_up()
            try:
                def read_from_other_loop():
                    async def read_state():
                        return await brain.get_state()

                    try:
                        asyncio.run(read_state())
                    except RuntimeError as exc:
                        return str(exc)
                    return "no error"

                message = await asyncio.to_thread(read_from_other_loop)
                self.assertIn("event loop", message)
            finally:
                await brain.sleep()

    async def test_bridge_returns_when_feedback_cannot_be_delivered(self):
        """A stopped stem must not make user-message handling poll forever."""
        stem = BrainStem()
        stem._has_started = True
        await stem.stop()
        bridge = AgentBridge(stem, registry)
        result = await asyncio.wait_for(
            bridge.process_user_message("hello", source="user"),
            timeout=0.5,
        )
        self.assertEqual(result["type"], "error")

    async def test_agent_bridge_blocks_side_effects_by_default(self):
        stem = BrainStem()
        bridge = AgentBridge(stem, registry)
        fed_back = []

        async def capture(text, source="agent"):
            fed_back.append((text, source))

        bridge._feed_brain = capture
        from brain.intent import Intent, IntentType

        intent = Intent(
            type=IntentType.CALL_TOOL,
            tool_name="send_message",
            tool_args={"text": "must not be sent"},
            confidence=1.0,
        )
        await bridge._handle_call_tool(intent)
        self.assertEqual(len(fed_back), 1)
        self.assertIn("显式授权", fed_back[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
