"""Contract tests for the external P7 controlled host.

These tests cover only host-boundary helpers.  They never call a remote model
and never treat a fixed/fake candidate as a successful iteration.
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import inspect
import os
from dataclasses import replace
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


class P7ControlledHostContractTests(unittest.TestCase):
    @staticmethod
    def _legacy_drive_engine_source() -> str:
        """Build the pre-iteration fixture used by P7 gap-contract tests."""

        source = (Path.cwd() / "brain" / "drive_engine.py").read_text(
            encoding="utf-8"
        )
        approved_block = (
            "            entity = entities_pool[\n"
            "                int(hashlib.sha256((str(current_tick) + drive_name).encode(\"utf-8\")).hexdigest(), 16)\n"
            "                % len(entities_pool)\n"
            "            ] if entities_pool else \"未知领域\"\n"
        )
        legacy_line = (
            "            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)]"
            " if entities_pool else \"未知领域\"\n"
        )
        if "import hashlib\n" not in source or approved_block not in source:
            raise AssertionError("repository no longer contains the validated iteration target")
        return source.replace("import hashlib\n", "", 1).replace(
            approved_block, legacy_line, 1
        )

    def test_normalized_container_contract_digest_matches_wrapper_shape(self):
        import tools.p7_controlled_host as host_module

        contract = {
            "network": True, "filesystem": True, "privilege": True,
            "resources": True, "image": True, "identity": True,
            "ownership": True, "mount_count": 2,
        }
        normalized = host_module._normalized_container_contract(contract)
        expected = {key: True for key in (
            "network", "filesystem", "privilege", "resources",
            "image", "identity", "ownership",
        )}
        self.assertEqual(normalized, expected)
        self.assertEqual(
            host_module._digest(host_module._canonical(normalized)),
            host_module._digest(host_module._canonical(expected)),
        )

    @staticmethod
    def _bounded_result(value):
        """Adapt a CompletedProcess/timeout double to the bounded host runner."""
        import tools.p7_controlled_host as host_module

        if isinstance(value, subprocess.TimeoutExpired):
            return host_module._BoundedCommandResult(
                None, b"", b"", False, True, True
            )
        return host_module._BoundedCommandResult(
            getattr(value, "returncode", None),
            getattr(value, "stdout", b"") or b"",
            getattr(value, "stderr", b"") or b"",
            False,
            False,
            True,
        )

    @staticmethod
    def _fixture_docker_cli(root: Path) -> str:
        """Create an explicit executable stand-in for fixture-only tests."""

        name = "docker.exe" if os.name == "nt" else "docker"
        path = root / name
        path.write_bytes(b"fixture docker cli")
        if os.name != "nt":
            path.chmod(0o700)
        return str(path)

    def _run_generated_judge(self, root: Path, target_source: str):
        import tools.p7_controlled_host as host_module

        candidate = root / "candidate"
        brain = candidate / "brain"
        brain.mkdir(parents=True)
        (brain / "__init__.py").write_text("", encoding="utf-8")
        target = brain / "drive_engine.py"
        target.write_text(target_source, encoding="utf-8")
        layout = host_module.RunLayout.create(root)
        host = host_module.P7ControlledHost(repo_root=Path.cwd(), run_root=root)
        host._write_fixture(
            layout,
            image_ref="python@sha256:" + "a" * 64,
            context_name="default",
            docker_cli=self._fixture_docker_cli(root),
        )
        config = json.loads(
            (layout.fixtures / "executor.json").read_text(encoding="utf-8")
        )
        request = {
            "schema_version": 1,
            "protocol": host_module._SANDBOX_PROTOCOL,
            "nonce": "f" * 32,
            "candidate_target_sha256": hashlib.sha256(
                target.read_bytes()
            ).hexdigest(),
            "judge_sha256": config["judge_sha256"],
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "Path", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT"}
        }
        env["P7_EXECUTION_REQUEST"] = json.dumps(request, separators=(",", ":"))
        return host_module._run_bounded_command(
            [
                sys.executable,
                str(layout.fixtures / "judge.py"),
                str(candidate),
            ],
            env=env,
            cwd=layout.fixtures,
            timeout=15,
            limit=65536,
        )

    @staticmethod
    def _host_evaluation_receipt(
        *, isolated: bool = True, execution_request=None, exit_code: int | None = 0,
        timed_out: bool = False, startup_timed_out=None,
        boundary_closed: bool = True, startup_passed: bool = True,
        result_verified: bool = True, error: str = "", metadata=None,
        candidate_fingerprint: str = "c" * 64,
        baseline_fingerprint: str = "b" * 64,
        fixture_hash: str = "f" * 64,
        evaluator_hash: str = "e" * 64,
    ):
        from brain.evaluation_harness import EvaluationReceipt, GateResult

        startup_evidence = {
            "boundary_closed": boundary_closed,
            "exit_code": exit_code,
            "timed_out": timed_out if startup_timed_out is None else startup_timed_out,
        }
        if execution_request is not None:
            from tools.p7_controlled_host import _digest, _canonical

            startup_evidence["execution_request_digest"] = _digest(
                _canonical(execution_request)
            )

        return EvaluationReceipt(
            receipt_id="receipt-formal-test",
            harness_version="test",
            fixture_hash=fixture_hash,
            evaluator_hash=evaluator_hash,
            mode="evolution",
            candidate_revision_id="candidate",
            candidate_fingerprint=candidate_fingerprint,
            baseline_revision_id="baseline",
            baseline_fingerprint=baseline_fingerprint,
            baseline_metrics={"quality": 1.0},
            candidate_metrics={"quality": 2.0},
            primary_dimension="quality",
            improvement=1.0,
            gates=(
                GateResult(
                    name="startup",
                    passed=startup_passed,
                    evidence=startup_evidence,
                ),
            ),
            accepted=True,
            outcome="accepted",
            rollback_performed=False,
            rollback_target="baseline",
            isolated=isolated,
            started_at="2026-09-12T00:00:00+00:00",
            finished_at="2026-09-12T00:00:01+00:00",
            duration_sec=1.0,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_bytes=0,
            stderr_bytes=0,
            result_verified=result_verified,
            error=error,
            metadata=metadata if metadata is not None else {
                "inspect_verified": True,
                "cleanup_verified": True,
            },
        )

    @staticmethod
    def _formal_request(host_module, endpoint: str, *, nonce: str) -> dict:
        return {
            "nonce": nonce,
            "endpoint_digest": host_module._digest(endpoint),
            "candidate_target_sha256": "a" * 64,
            "executor_config_sha256": "b" * 64,
            "wrapper_sha256": "c" * 64,
            "judge_sha256": "d" * 64,
            "candidate_fingerprint": "c" * 64,
            "baseline_fingerprint": "b" * 64,
            "fixture_hash": "f" * 64,
            "evaluator_hash": "e" * 64,
            "sandbox_contract_digest": "1" * 64,
            "container_config_digest": "2" * 64,
            "image_id_digest": "3" * 64,
            "resource_budget": {},
        }

    @staticmethod
    def _formal_harness():
        return SimpleNamespace(
            harness_version="test",
            fixture_hash="f" * 64,
            evaluator_hash="e" * 64,
        )

    @staticmethod
    def _formal_execution_metadata(host_module, request: dict) -> dict:
        metadata = {
            "schema_version": host_module.HOST_SCHEMA_VERSION,
            "protocol": host_module._SANDBOX_PROTOCOL,
            "nonce": request["nonce"],
            "request_digest": host_module._digest(host_module._canonical(request)),
            "input_integrity_verified": True,
            "candidate_target_sha256_before": request["candidate_target_sha256"],
            "candidate_target_sha256_after": request["candidate_target_sha256"],
            "judge_sha256_before": request["judge_sha256"],
            "judge_sha256_after": request["judge_sha256"],
            "inspect_verified": True,
            "cleanup_verified": True,
        }
        for key in (
            "wrapper_sha256", "judge_sha256", "executor_config_sha256",
            "container_config_digest", "image_id_digest", "endpoint_digest",
            "sandbox_contract_digest",
        ):
            metadata[key] = request[key]
        return metadata

    def test_generated_judge_bounds_child_output(self):
        source = (
            "print('x' * 131072)\n"
            "class Goal:\n"
            "    description = 'ok'\n"
            "class GoalGenerator:\n"
            "    def generate(self, *args):\n"
            "        return [Goal()]\n"
        )
        with tempfile.TemporaryDirectory(prefix="p7-judge-output-") as temp:
            result = self._run_generated_judge(Path(temp), source)
        payload = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["error"], "probe_output_overflow")
        self.assertLess(len(result.stdout), 1024)

    def test_generated_judge_bounds_child_timeout(self):
        source = (
            "import time\n"
            "time.sleep(30)\n"
            "class Goal:\n"
            "    description = 'ok'\n"
            "class GoalGenerator:\n"
            "    def generate(self, *args):\n"
            "        return [Goal()]\n"
        )
        with tempfile.TemporaryDirectory(prefix="p7-judge-timeout-") as temp:
            result = self._run_generated_judge(Path(temp), source)
        payload = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["error"], "probe_child_timeout")

    def test_generated_judge_reaps_descendants_after_normal_parent_exit(self):
        """A candidate cannot keep the judge pipe or temp tree open."""
        source = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
            "class Goal:\n"
            "    description = 'ok'\n"
            "class GoalGenerator:\n"
            "    def generate(self, *args):\n"
            "        return [Goal()]\n"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="p7-judge-descendant-") as temp:
            result = self._run_generated_judge(Path(temp), source)
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertTrue(result.drained)
        payload = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(payload["verified"])

    def test_host_bounded_command_reaps_descendants_after_normal_parent_exit(self):
        import tools.p7_controlled_host as host_module

        code = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
            "print('ok')\n"
        )
        started = time.monotonic()
        result = host_module._run_bounded_command(
            [sys.executable, "-c", code], timeout=3, limit=4096
        )
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"ok")
        self.assertTrue(result.drained)

    def test_bounded_success_rejects_uncertain_completion_state(self):
        import tools.p7_controlled_host as host_module

        complete = host_module._BoundedCommandResult(
            0, b"ok", b"", False, False, True
        )
        self.assertTrue(host_module._bounded_success(complete))
        for uncertain in (
            host_module._BoundedCommandResult(0, b"ok", b"", False, True, True),
            host_module._BoundedCommandResult(0, b"ok", b"", True, False, True),
            host_module._BoundedCommandResult(0, b"ok", b"", False, False, False),
        ):
            self.assertFalse(host_module._bounded_success(uncertain))

        with mock.patch.object(
            host_module.subprocess,
            "Popen",
            side_effect=OSError("spawn failed"),
        ):
            spawn_failed = host_module._run_bounded_command(
                ["missing-command"], timeout=1
            )
        self.assertIsNone(spawn_failed.returncode)
        self.assertFalse(spawn_failed.drained)

        class _Process:
            pid = 12345

            def kill(self):
                return None

        # Permission failures are not equivalent to an already-disappeared
        # process group.  The host must keep the boundary untrusted.
        with mock.patch.object(host_module.os, "name", "posix"), mock.patch.object(
            host_module.signal, "SIGKILL", 9, create=True
        ), mock.patch.object(
            host_module.os,
            "killpg",
            side_effect=PermissionError("denied"),
            create=True,
        ):
            self.assertFalse(
                host_module._terminate_process_tree(_Process(), process_group=12345)
            )
        with mock.patch.object(host_module.os, "name", "posix"), mock.patch.object(
            host_module.signal, "SIGKILL", 9, create=True
        ), mock.patch.object(
            host_module.os,
            "killpg",
            side_effect=ProcessLookupError("gone"),
            create=True,
        ):
            self.assertTrue(
                host_module._terminate_process_tree(_Process(), process_group=12345)
            )

    def test_bounded_setup_interrupt_closes_process_and_pipes_before_reraising(self):
        """A setup exception cannot strand the child outside the host boundary."""

        import tools.p7_controlled_host as host_module

        class _Stream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Process:
            pid = 12345

            def __init__(self):
                self.stdout = _Stream()
                self.stderr = _Stream()
                self.returncode = -9
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                return self.returncode

        process = _Process()
        with mock.patch.object(
            host_module.subprocess, "Popen", return_value=process
        ), mock.patch.object(
            host_module, "_create_kill_job", side_effect=KeyboardInterrupt("setup")
        ), mock.patch.object(
            host_module, "_terminate_process_tree", return_value=True
        ) as terminate:
            with self.assertRaises(KeyboardInterrupt):
                host_module._run_bounded_command(
                    ["fixture-command"], timeout=1, capture_stderr=True
                )
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        terminate.assert_called_once()

    def test_deepseek_channel_tracks_the_current_official_flash_endpoint(self):
        import services.llm_client as llm

        self.assertEqual(llm.DEEPSEEK_MODEL, "deepseek-v4-flash")
        self.assertEqual(llm.DEEPSEEK_BASE, "https://api.deepseek.com")

    def test_deepseek_authorization_binding_uses_only_canonical_model_id(self):
        """The host binding must follow the client and reject the stale alias."""
        import inspect
        import services.llm_client as llm
        import tools.p7_controlled_host as host_module

        self.assertEqual(llm.DEEPSEEK_MODEL, "deepseek-v4-flash")
        host_source = inspect.getsource(host_module)
        self.assertIn('"deepseek-v4-flash"', host_source)
        self.assertNotIn('"deepseek-flash"', host_source)

    def test_docker_environment_preserves_context_location_hints_only(self):
        import tools.p7_controlled_host as host_module

        with mock.patch.dict(
            host_module.os.environ,
            {
                "USERPROFILE": r"C:\Users\operator",
                "APPDATA": r"C:\Users\operator\AppData\Roaming",
                "DOCKER_CONFIG": r"C:\Users\operator\.docker",
                "DOCKER_API_KEY": "must-not-cross-boundary",
                "HTTPS_PROXY": "https://proxy.invalid",
            },
            clear=False,
        ):
            env = host_module._safe_docker_environment()
        self.assertEqual(env["USERPROFILE"], r"C:\Users\operator")
        self.assertEqual(env["APPDATA"], r"C:\Users\operator\AppData\Roaming")
        self.assertEqual(env["DOCKER_CONFIG"], r"C:\Users\operator\.docker")
        self.assertNotIn("DOCKER_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_model_patch_validation_allows_only_the_declared_low_risk_file(self):
        from tools.p7_controlled_host import validate_model_patch

        patch = """--- a/brain/drive_engine.py
+++ b/brain/drive_engine.py
@@ -1,2 +1,3 @@
 import logging
+import hashlib
 from datetime import datetime, timezone
@@ -375,1 +376,1 @@
-            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)] if entities_pool else "未知领域"
+            entity = entities_pool[int.from_bytes(hashlib.sha256((str(current_tick) + drive_name).encode("utf-8")).digest()[:8], "big") % len(entities_pool)] if entities_pool else "未知领域"
"""
        parsed = validate_model_patch(
            {
                "title": "stable replay",
                "scope": "brain/drive_engine.py",
                "hypothesis": "stable digest removes cross-process hash randomization",
                "unified_diff": patch,
            }
        )
        self.assertEqual(parsed.scope, "brain/drive_engine.py")
        self.assertIn("unified_diff", parsed.to_dict())

    def test_iteration_target_registry_is_allowlisted_and_path_free(self):
        import tools.p7_controlled_host as host_module

        target = host_module.resolve_iteration_target("goal_determinism")
        self.assertEqual(target.scope, host_module.TARGET_SCOPE)
        self.assertEqual(target.public_summary()["target_id"], "goal_determinism")
        self.assertRegex(target.public_summary()["contract_digest"], r"^[0-9a-f]{64}$")
        with self.assertRaises(ValueError):
            host_module.resolve_iteration_target("brain/brain_stem.py")
        with self.assertRaises(ValueError):
            host_module.resolve_iteration_target("../brain/drive_engine.py")
        self.assertNotIn("\\", target.scope)
        self.assertNotIn("/D:", target.scope)

    def test_iteration_target_binding_rejects_contract_drift(self):
        import tools.p7_controlled_host as host_module

        host = host_module.P7ControlledHost(repo_root=Path.cwd())
        summary = host._target_summary()
        self.assertTrue(host._target_binding_matches(summary))
        tampered = dict(summary, contract_digest="0" * 64)
        self.assertFalse(host._target_binding_matches(tampered))
        public = host_module.public_report({"target": summary})
        self.assertEqual(public["target"]["target_id"], "goal_determinism")
        self.assertEqual(public["target"]["scope"], host_module.TARGET_SCOPE)

    def test_model_patch_validation_rejects_scope_escape_and_multiple_files(self):
        from tools.p7_controlled_host import validate_model_patch

        with self.assertRaises(ValueError):
            validate_model_patch(
                {
                    "title": "bad",
                    "scope": "brain/brain_stem.py",
                    "hypothesis": "rewrite policy",
                    "unified_diff": "--- a/brain/brain_stem.py\n+++ b/brain/brain_stem.py\n@@ -1 +1 @@\n-a\n+b\n",
                }
            )

    def test_model_patch_validation_rejects_paths_and_urls_in_prose(self):
        from tools.p7_controlled_host import validate_model_patch

        patch = """--- a/brain/drive_engine.py
+++ b/brain/drive_engine.py
@@ -1,2 +1,3 @@
 import logging
+import hashlib
 from datetime import datetime, timezone
@@ -375,1 +376,1 @@
-            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)] if entities_pool else "x"
+            entity = entities_pool[int(hashlib.sha256((str(current_tick) + drive_name).encode("utf-8")).hexdigest(), 16) % len(entities_pool)] if entities_pool else "x"
"""
        for title, hypothesis in (
            ("see /workspace/private", "stable digest"),
            ("stable replay", "details at https://example.invalid/private"),
        ):
            with self.assertRaisesRegex(ValueError, "path or secret"):
                validate_model_patch(
                    {
                        "title": title,
                        "scope": "brain/drive_engine.py",
                        "hypothesis": hypothesis,
                        "unified_diff": patch,
                    }
                )
        with self.assertRaises(ValueError):
            validate_model_patch(
                {
                    "title": "bad",
                    "scope": "brain/drive_engine.py",
                    "hypothesis": "x",
                    "unified_diff": (
                        "--- a/brain/drive_engine.py\n+++ b/brain/drive_engine.py\n"
                        "@@ -1 +1 @@\n-a\n+b\n"
                        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
                    ),
                }
            )

    def test_public_report_drops_paths_ledgers_and_secret_shaped_values(self):
        from tools.p7_controlled_host import public_report

        report = public_report(
            {
                "active_path": r"C:\\private\\active",
                "candidate_path": r"C:\\private\\candidate",
                "workspace_root": r"C:\\private",
                "ledger": [{"event": "secret"}],
                "external_ledger_bound": True,
                "api_key": "sk-test-not-for-output",
                "nested": {"source_path": "/tmp/source", "ready": True},
                "generic_path": "/workspace/private/candidate.py",
                "endpoint": "https://example.invalid/private",
                "deepseek_api_key": "placeholder-secret-value",
                "diagnostic": r"failed at C:\private\candidate\file.py",
                "need_binding": {
                    "proofs": [
                        {
                            "event_id": "p7-private-event",
                            "replay_key": "a" * 64,
                            "attestation_hash": "b" * 64,
                        }
                    ],
                    "proof_count": 1,
                    "binding_digest": "c" * 64,
                },
            }
        )
        encoded = json.dumps(report, ensure_ascii=False)
        for forbidden in (
            "active_path",
            "candidate_path",
            "workspace_root",
            "source_path",
            '"ledger"',
            "api_key",
            "sk-test",
            "C:\\private",
            "/tmp/source",
            "/workspace/private/candidate.py",
            "https://example.invalid/private",
            "deepseek_api_key",
            "failed at",
            "p7-private-event",
            '"proofs"',
            '"replay_key"',
            '"attestation_hash"',
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertTrue(report["external_ledger_bound"])
        self.assertEqual(report["need_binding"]["proof_count"], 1)
        self.assertEqual(report["need_binding"]["binding_digest"], "c" * 64)

    def test_private_run_metadata_is_separate_from_the_public_receipt(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-private-metadata-") as temp:
            layout = RunLayout.create(Path(temp))
            host = P7ControlledHost(
                repo_root=Path.cwd(),
                run_root=Path(temp),
                manifest_key="k" * 32,
            )
            report = {
                "schema_version": 1,
                "run_id": layout.run_id,
                "phase": "awaiting_authorization",
                "need_binding": {
                    "proofs": [{"replay_key": "a" * 64}],
                    "proof_count": 1,
                    "binding_digest": "b" * 64,
                },
            }
            host._save_report(layout, report)

            public = json.loads(layout.metadata.read_text(encoding="utf-8"))
            private = json.loads(layout.private_metadata.read_text(encoding="utf-8"))

            self.assertNotIn("proofs", public["need_binding"])
            self.assertEqual(public["need_binding"]["proof_count"], 1)
            self.assertEqual(private["need_binding"]["proofs"][0]["replay_key"], "a" * 64)
            self.assertEqual(host._load_metadata(layout), private)

    def test_manifest_authentication_detects_private_public_and_manifest_tampering(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-manifest-tamper-") as temp:
            root = Path(temp)
            layout = RunLayout.create(root)
            host = P7ControlledHost(
                repo_root=Path.cwd(), run_root=root, manifest_key="m" * 32
            )
            report = {
                "schema_version": 1,
                "run_id": layout.run_id,
                "phase": "awaiting_authorization",
                "authorization_required": True,
                "marker": "stable",
            }
            host._save_report(layout, report)

            private = json.loads(layout.private_metadata.read_text(encoding="utf-8"))
            private["marker"] = "tampered"
            layout.private_metadata.write_text(
                json.dumps(private), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "metadata"):
                host._load_metadata(layout)

            host._save_report(layout, report)
            public = json.loads(layout.metadata.read_text(encoding="utf-8"))
            public["marker"] = "tampered"
            layout.metadata.write_text(json.dumps(public), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata"):
                host._load_metadata(layout)

            host._save_report(layout, report)
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            manifest["mac"] = "0" * 64
            layout.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authentication"):
                host._load_metadata(layout)

    def test_authorization_claim_is_single_use_and_release_reopens_with_manifest(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-authorization-cas-") as temp:
            root = Path(temp)
            layout = RunLayout.create(root)
            host = P7ControlledHost(
                repo_root=Path.cwd(), run_root=root, manifest_key="c" * 32
            )
            host._save_report(
                layout,
                {
                    "schema_version": 1,
                    "run_id": layout.run_id,
                    "phase": "awaiting_authorization",
                    "authorization_required": True,
                    "evidence_marker": "retained",
                },
            )
            claimed, reason = host._claim_authorization(layout)
            self.assertEqual(reason, "")
            self.assertIsNotNone(claimed)
            blocked, blocked_reason = host._claim_authorization(layout)
            self.assertIsNone(blocked)
            self.assertEqual(blocked_reason, "authorization_in_progress")

            released = host._release_authorization(
                layout, claimed, reason="transient_executor_unavailable"
            )
            self.assertEqual(released["phase"], "awaiting_authorization")
            self.assertEqual(host._load_metadata(layout)["phase"], "awaiting_authorization")

            claimed_again, reason_again = host._claim_authorization(layout)
            self.assertEqual(reason_again, "")
            terminal = {
                "schema_version": 1,
                "run_id": layout.run_id,
                "phase": "blocked",
                "authorization_required": True,
                "reason": "test_terminal",
                "authorization_claim_id": claimed_again["authorization_claim_id"],
            }
            host._finish_authorization(layout, terminal)
            self.assertEqual(host._load_metadata(layout)["phase"], "blocked")
            self.assertEqual(
                host._load_metadata(layout)["evidence_marker"], "retained"
            )
            blocked_again, blocked_again_reason = host._claim_authorization(layout)
            self.assertIsNone(blocked_again)
            self.assertEqual(blocked_again_reason, "run_is_not_awaiting_authorization")

    def test_ephemeral_manifest_key_fails_closed_across_host_instances(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        # Keep this contract independent of a developer's persistent CLI key.
        # The scenario under test is explicitly the in-process ephemeral-key
        # path, not the production two-phase environment configuration.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRAIN_MEMORY_P7_MANIFEST_KEY", None)
            with tempfile.TemporaryDirectory(prefix="p7-ephemeral-key-") as temp:
                root = Path(temp)
                layout = RunLayout.create(root)
                first = P7ControlledHost(repo_root=Path.cwd(), run_root=root)
                first._save_report(
                    layout,
                    {
                        "schema_version": 1,
                        "run_id": layout.run_id,
                        "phase": "awaiting_authorization",
                    },
                )
                second = P7ControlledHost(repo_root=Path.cwd(), run_root=root)
                with self.assertRaisesRegex(ValueError, "authentication"):
                    second._load_metadata(layout)

    def test_persistent_manifest_key_derives_stable_domain_separated_run_secrets(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-run-secrets-") as temp:
            root = Path(temp)
            layout = RunLayout.create(root)
            first = P7ControlledHost(
                repo_root=Path.cwd(), run_root=root, manifest_key="s" * 32
            )
            second = P7ControlledHost(
                repo_root=Path.cwd(), run_root=root, manifest_key="s" * 32
            )
            self.assertEqual(
                first._run_secret(layout, "sandbox-attestation"),
                second._run_secret(layout, "sandbox-attestation"),
            )
            self.assertNotEqual(
                first._run_secret(layout, "sandbox-attestation"),
                first._run_secret(layout, "motivation-source"),
            )

    def test_atomic_json_writer_refuses_a_hard_link_target(self):
        from tools.p7_controlled_host import _write_json

        with tempfile.TemporaryDirectory(prefix="p7-json-hardlink-") as temp:
            root = Path(temp)
            original = root / "outside.json"
            target = root / "run.json"
            original.write_text("preserve", encoding="utf-8")
            try:
                os.link(original, target)
            except OSError:
                self.skipTest("hard links are not available on this filesystem")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                _write_json(target, {"changed": True})
            self.assertEqual(original.read_text(encoding="utf-8"), "preserve")

    def test_run_layout_rejects_a_dangling_metadata_link(self):
        from tools.p7_controlled_host import RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-layout-dangling-") as temp:
            root = Path(temp)
            layout = RunLayout.create(root)
            dangling = layout.root / "state.sqlite"
            try:
                dangling.symlink_to(root / "does-not-exist")
            except OSError:
                self.skipTest("symlinks are not available on this filesystem")
            with self.assertRaisesRegex(ValueError, "metadata"):
                RunLayout.load(layout.run_id, root)

    def test_docker_resolution_never_falls_back_to_path(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-docker-cli-") as temp:
            root = Path(temp)
            explicit = Path(self._fixture_docker_cli(root))
            with mock.patch.dict(
                os.environ,
                {host_module.DOCKER_CLI_ENV: str(explicit)},
                clear=False,
            ):
                self.assertEqual(
                    host_module._configured_docker_cli(), str(explicit.resolve())
                )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(host_module.DOCKER_CLI_ENV, None)
                with mock.patch.object(host_module.shutil, "which", return_value=str(explicit)):
                    self.assertEqual(host_module._configured_docker_cli(), "")

    def test_docker_resolution_rejects_aliases_and_non_absolute_values(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-docker-cli-alias-") as temp:
            root = Path(temp)
            explicit = Path(self._fixture_docker_cli(root))
            self.assertEqual(host_module._configured_docker_cli(str(explicit)), str(explicit.resolve()))
            self.assertEqual(host_module._configured_docker_cli("docker.exe"), "")
            alias = root / ("alias.exe" if os.name == "nt" else "alias")
            try:
                alias.symlink_to(explicit)
            except OSError:
                self.skipTest("symlinks are not available on this filesystem")
            self.assertEqual(host_module._configured_docker_cli(str(alias)), "")

    def test_fake_model_is_never_coerced_into_a_candidate(self):
        from tools.p7_controlled_host import coerce_model_patch

        self.assertIsNone(coerce_model_patch({"status": "offline"}))
        self.assertIsNone(coerce_model_patch({"unified_diff": ""}))

    def test_replay_probe_reproduces_the_baseline_cross_process_gap(self):
        from tools.p7_controlled_host import replay_probe

        with tempfile.TemporaryDirectory(prefix="p7-probe-contract-") as temp:
            root = Path(temp)
            import tools.p7_controlled_host as host_module

            checkout = root / "checkout"
            host_module._safe_copy_tree(Path.cwd(), checkout)
            (checkout / "brain" / "drive_engine.py").write_text(
                self._legacy_drive_engine_source(), encoding="utf-8"
            )
            result = replay_probe(checkout)
        self.assertEqual(result["source_kind"], "runtime_replay")
        self.assertEqual(result["seed_count"], 8)
        self.assertGreaterEqual(result["unique_description_count"], 2)
        self.assertFalse(result["replayable"])

    def test_low_risk_transform_requires_the_exact_deterministic_index_ast(self):
        from tools.p7_controlled_host import _assert_low_risk_transform

        baseline_source = self._legacy_drive_engine_source()
        old_index = "hash(str(current_tick) + drive_name) % len(entities_pool)"
        approved_index = (
            "int(hashlib.sha256((str(current_tick) + drive_name).encode(\"utf-8\")).hexdigest(), 16)"
            " % len(entities_pool)"
        )
        variants = {
            "approved": approved_index,
            "plus_one": approved_index.replace(
                " % len(entities_pool)", " + 1) % len(entities_pool)"
            ).replace("int(hashlib", "(int(hashlib", 1),
            "zero_times": approved_index.replace(
                " % len(entities_pool)", " * 0) % len(entities_pool)"
            ).replace("int(hashlib", "(int(hashlib", 1),
        }
        self.assertIn(old_index, baseline_source)
        with tempfile.TemporaryDirectory(prefix="p7-ast-contract-") as temp:
            root = Path(temp)
            baseline = root / "baseline"
            candidate = root / "candidate"
            (baseline / "brain").mkdir(parents=True)
            (candidate / "brain").mkdir(parents=True)
            (baseline / "brain" / "drive_engine.py").write_text(
                baseline_source, encoding="utf-8"
            )
            for name, index in variants.items():
                candidate_source = baseline_source.replace(
                    "import logging", "import logging\nimport hashlib", 1
                ).replace(old_index, index, 1)
                (candidate / "brain" / "drive_engine.py").write_text(
                    candidate_source, encoding="utf-8"
                )
                if name == "approved":
                    _assert_low_risk_transform(baseline, candidate)
                else:
                    with self.assertRaisesRegex(RuntimeError, "approved deterministic"):
                        _assert_low_risk_transform(baseline, candidate)

    def test_model_diff_is_canonicalized_without_trusting_damaged_context(self):
        from tools.p7_controlled_host import (
            TARGET_SCOPE,
            _apply_unified_diff,
            _canonical_model_patch,
            validate_model_patch,
        )
        from brain.evaluation_harness import BaselineRevision, CandidateRevision

        raw_diff = '''--- a/brain/drive_engine.py
+++ b/brain/drive_engine.py
@@ -16,1 +16,2 @@
 import logging
+import hashlib
@@ -375,1 +376,1 @@
-            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)] if entities_pool else "corrupted"
+            entity = entities_pool[int(hashlib.sha256((str(current_tick) + drive_name).encode("utf-8")).hexdigest(), 16) % len(entities_pool)] if entities_pool else "corrupted"
'''
        patch = validate_model_patch(
            {
                "title": "stable replay",
                "scope": TARGET_SCOPE,
                "hypothesis": "replace process-randomized hash with a stable digest",
                "unified_diff": raw_diff,
            }
        )
        baseline_source = self._legacy_drive_engine_source()
        with tempfile.TemporaryDirectory(prefix="p7-canonical-diff-") as temp:
            root = Path(temp)
            baseline = root / "baseline"
            candidate = root / "candidate"
            for tree in (baseline, candidate):
                (tree / "brain").mkdir(parents=True)
                (tree / TARGET_SCOPE).write_text(baseline_source, encoding="utf-8")
            canonical = _canonical_model_patch(patch, baseline)
            self.assertEqual(canonical.model_response_hash, patch.diff_hash)
            self.assertNotEqual(canonical.diff_hash, patch.diff_hash)
            _apply_unified_diff(candidate, canonical, baseline=baseline)
            baseline_revision = BaselineRevision.from_path(baseline)
            candidate_revision = CandidateRevision.from_path(candidate)
            self.assertNotEqual(
                candidate_revision.fingerprint,
                baseline_revision.fingerprint,
            )
            result = (candidate / TARGET_SCOPE).read_text(encoding="utf-8")
        self.assertIn("import hashlib", result)
        self.assertIn('else "未知领域"', result)
        self.assertNotIn("corrupted", result)

    def test_model_diff_canonicalizer_rejects_a_deterministic_constant_bypass(self):
        from tools.p7_controlled_host import _canonical_model_patch, validate_model_patch

        raw_diff = '''--- a/brain/drive_engine.py
+++ b/brain/drive_engine.py
@@ -16,1 +16,2 @@
 import logging
+import hashlib
@@ -375,1 +376,1 @@
-            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)] if entities_pool else "x"
+            entity = entities_pool[(0 * int(hashlib.sha256((str(current_tick) + drive_name).encode("utf-8")).hexdigest(), 16)) % len(entities_pool)] if entities_pool else "x"
'''
        patch = validate_model_patch(
            {
                "title": "bad",
                "scope": "brain/drive_engine.py",
                "hypothesis": "looks deterministic but collapses behavior",
                "unified_diff": raw_diff,
            }
        )
        with tempfile.TemporaryDirectory(prefix="p7-canonical-bypass-") as temp:
            baseline = Path(temp) / "baseline"
            (baseline / "brain").mkdir(parents=True)
            (baseline / "brain" / "drive_engine.py").write_text(
                self._legacy_drive_engine_source(), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "approved deterministic"):
                _canonical_model_patch(patch, baseline)

    def test_need_binding_payload_preserves_normalized_created_at(self):
        from brain.motivation import IterationNeed
        from tools.p7_controlled_host import P7ControlledHost

        first = IterationNeed(
            motive="growth",
            pressure=0.8,
            trigger="persistent_pressure",
            reason="verified gap",
            evidence=("evidence",),
            source_labels=("host",),
            urgency=0.8,
            confidence=0.9,
            created_at="2026-01-01T00:00:00+00:00",
            need_id="need-stable",
        )
        first_binding = P7ControlledHost._need_payload(first)
        summary = P7ControlledHost()._need_summary(first)
        self.assertEqual(summary["need_id"], "need-stable")
        self.assertEqual(summary["evidence_count"], 1)
        self.assertEqual(summary["source_count"], 1)
        restored = IterationNeed.from_dict(
            {**P7ControlledHost._need_payload(first), "requires_evaluation": True}
        )
        self.assertIsNotNone(restored)
        self.assertEqual(
            first_binding,
            P7ControlledHost._need_payload(restored),
        )
        tampered = {**P7ControlledHost._need_payload(first), "created_at": "2026-01-02T00:00:00+00:00"}
        changed = IterationNeed.from_dict(tampered)
        self.assertIsNotNone(changed)
        self.assertNotEqual(first_binding, P7ControlledHost._need_payload(changed))

    def test_authorization_failure_cleans_up_without_unbound_restart_state(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-auth-cleanup-") as temp:
            docker_cli = self._fixture_docker_cli(Path(temp))
            layout = RunLayout.create(Path(temp))
            host = P7ControlledHost(
                repo_root=Path.cwd(),
                run_root=Path(temp),
                manifest_key="k" * 32,
            )
            host._write_fixture(
                layout,
                image_ref="python@sha256:" + "a" * 64,
                context_name="default",
                docker_cli=docker_cli,
            )
            with mock.patch.object(
                RunLayout, "load", return_value=layout
            ), mock.patch.object(
                host,
                "_claim_authorization",
                return_value=(
                    {
                        "run_id": layout.run_id,
                        "phase": "authorizing",
                        "authorization_claim_id": "a" * 32,
                        "target": host._target_summary(),
                    },
                    "",
                ),
            ), mock.patch.object(
                host,
                "_load_metadata",
                return_value={
                    "run_id": layout.run_id,
                    "phase": "authorizing",
                    "authorization_claim_id": "a" * 32,
                    "target": host._target_summary(),
                },
            ), mock.patch(
                "tools.p7_controlled_host._sandbox_capability_report",
                return_value={
                    "ready": True,
                    "executor": "test",
                    "daemon_reachable": True,
                    "network_isolation_verified": True,
                    "filesystem_isolation_verified": True,
                    "reason": "",
                },
            ), mock.patch.object(
                host, "_build_runtime", side_effect=RuntimeError("forced test failure")
            ):
                result = asyncio.run(host.authorize_and_verify_async(layout.run_id))
            self.assertEqual(result["phase"], "blocked")
            self.assertEqual(result["reason"], "RuntimeError")
            source = inspect.getsource(P7ControlledHost.authorize_and_verify_async)
            self.assertIn("restarted: _Runtime | None = None", source)
            self.assertIn("replay_key=replay_key", source)

    def test_sandbox_capability_requires_a_digest_pinned_local_image(self):
        import tools.p7_controlled_host as host_module

        completed = mock.Mock(returncode=0)
        completed.stdout = b"29.0.0\n"
        completed.stderr = b""
        with mock.patch.object(host_module, "_configured_docker_cli", return_value=sys.executable), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value="npipe://local"
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            return_value=self._bounded_result(completed),
        ):
            report = host_module._sandbox_capability_report(
                "python:3.12-slim", "default"
            )
        self.assertFalse(report["ready"])
        self.assertTrue(report["daemon_reachable"])
        self.assertFalse(report["network_isolation_verified"])
        self.assertFalse(report["filesystem_isolation_verified"])
        self.assertEqual(report["reason"], "docker_image_not_pinned")

    def test_sandbox_capability_rejects_a_remote_docker_context(self):
        import tools.p7_controlled_host as host_module

        with mock.patch.object(host_module, "_configured_docker_cli", return_value=sys.executable), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=""
        ), mock.patch.object(host_module, "_run_bounded_command") as run:
            report = host_module._sandbox_capability_report(
                "python@sha256:" + "a" * 64, "remote"
            )

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason"], "docker_context_not_local")
        run.assert_not_called()

    def test_sandbox_capability_rejects_an_unbound_repo_digest(self):
        import tools.p7_controlled_host as host_module

        pinned = "python@sha256:" + "a" * 64
        version = mock.Mock(returncode=0, stdout=b"29.0.0\n", stderr=b"")
        inspected = mock.Mock(
            returncode=0,
            stdout=(
                "sha256:" + "b" * 64 + "|" + json.dumps(["python@sha256:" + "c" * 64])
            ).encode("utf-8"),
            stderr=b"",
        )
        with mock.patch.object(host_module, "_configured_docker_cli", return_value=sys.executable), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value="npipe://local"
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            side_effect=[self._bounded_result(version), self._bounded_result(inspected)],
        ):
            report = host_module._sandbox_capability_report(pinned, "default")

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason"], "docker_image_digest_mismatch")
        self.assertFalse(report["image_available"])

    def test_sandbox_capability_accepts_only_the_verified_docker_protocol(self):
        import tools.p7_controlled_host as host_module

        pinned = "python@sha256:" + "a" * 64
        version = mock.Mock(returncode=0, stdout=b"29.0.0\n", stderr=b"")
        image_id = "sha256:" + "b" * 64
        inspected = mock.Mock(
            returncode=0,
            stdout=(
                image_id + "|" + json.dumps(["python@sha256:" + "a" * 64]) + "\n"
            ).encode("utf-8"),
            stderr=b"",
        )
        created = mock.Mock(returncode=0, stdout=("c" * 64 + "\n").encode(), stderr=b"")
        container_contract = {
            "Id": "c" * 64,
            "Name": "/brain-memory-p7-probe-" + "d" * 32,
            "Image": image_id,
            "Config": {
                "Image": image_id,
                "User": "65534:65534",
                "Labels": {"brain-memory.p7.nonce": "d" * 32},
            },
            "HostConfig": {
                "NetworkMode": "none",
                "IpcMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 64,
                "Memory": 256 * 1024 * 1024,
                "MemorySwap": 256 * 1024 * 1024,
                "NanoCpus": 1_000_000_000,
                "LogConfig": {"Type": "none", "Config": {}},
                "ShmSize": 16 * 1024 * 1024,
                "Init": True,
                "Ulimits": [
                    {"Name": "nofile", "Soft": 256, "Hard": 256},
                    {"Name": "nproc", "Soft": 64, "Hard": 64},
                ],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=33554432,mode=1777"},
            },
            "Mounts": [{"Destination": "/probe", "Type": "bind", "RW": False}],
        }
        container_inspected = mock.Mock(
            returncode=0,
            stdout=json.dumps(container_contract).encode("utf-8"),
            stderr=b"",
        )
        probe_payload = {
            "schema_version": 1,
            "ready": True,
            "network_isolation_verified": True,
            "filesystem_isolation_verified": True,
            "privilege_isolation_verified": True,
            "temporary_write_verified": True,
        }
        probed = mock.Mock(
            returncode=0,
            stdout=(json.dumps(probe_payload) + "\n").encode("utf-8"),
            stderr=b"",
        )
        removed = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(host_module, "_configured_docker_cli", return_value=sys.executable), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value="npipe://local"
        ), mock.patch.object(
            host_module.secrets, "token_hex", return_value="d" * 32
        ), mock.patch.object(
            host_module, "_inspect_mount_contract", return_value=True
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            side_effect=[
                self._bounded_result(value)
                for value in (
                    version,
                    inspected,
                    created,
                    container_inspected,
                    probed,
                    removed,
                     mock.Mock(returncode=0, stdout=b"", stderr=b""),
                     mock.Mock(returncode=0, stdout=b"", stderr=b""),
                )
            ],
        ) as run:
            report = host_module._sandbox_capability_report(pinned, "default")

        self.assertTrue(report["ready"])
        self.assertTrue(report["image_pinned"])
        self.assertTrue(report["image_available"])
        self.assertTrue(report["probe_verified"])
        self.assertEqual(
            report["endpoint_digest"],
            hashlib.sha256(b"npipe://local").hexdigest(),
        )
        self.assertRegex(report["contract_digest"], r"^[0-9a-f]{64}$")
        command = run.call_args_list[2].args[0]
        for required in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory-swap=256m",
            "--log-driver=none",
            "--user=65534:65534",
            "--init",
            "--name=brain-memory-p7-probe-" + "d" * 32,
            "--label=brain-memory.p7.nonce=" + "d" * 32,
            "--entrypoint=python",
            pinned,
            "/probe/probe.py",
        ):
            self.assertIn(required, command)

        # Even an owned, fully inspected container must not be removed while
        # the start command's process/tree boundary is still uncertain.
        uncertain_start = host_module._BoundedCommandResult(
            -9, b"", b"", False, True, False
        )
        with mock.patch.object(
            host_module, "_configured_docker_cli", return_value=sys.executable
        ), mock.patch.object(
            host_module,
            "_local_docker_context_endpoint",
            return_value="npipe://local",
        ), mock.patch.object(
            host_module.secrets, "token_hex", return_value="d" * 32
        ), mock.patch.object(
            host_module, "_inspect_mount_contract", return_value=True
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            side_effect=[
                self._bounded_result(version),
                self._bounded_result(inspected),
                self._bounded_result(created),
                self._bounded_result(container_inspected),
                uncertain_start,
            ],
        ) as uncertain_run:
            uncertain_report = host_module._sandbox_capability_report(
                pinned, "default"
            )
        self.assertFalse(uncertain_report["ready"])
        self.assertFalse(
            any("rm" in call.args[0] for call in uncertain_run.call_args_list)
        )

        with mock.patch.object(
            host_module, "_configured_docker_cli", return_value=sys.executable
        ), mock.patch.object(
            host_module,
            "_local_docker_context_endpoint",
            return_value="npipe://local",
        ), mock.patch.object(
            host_module.secrets, "token_hex", return_value="d" * 32
        ), mock.patch.object(
            host_module, "_inspect_mount_contract", return_value=True
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            side_effect=[
                self._bounded_result(version),
                self._bounded_result(inspected),
                self._bounded_result(created),
                self._bounded_result(container_inspected),
                KeyboardInterrupt(),
            ],
        ) as interrupted_run:
            with self.assertRaises(KeyboardInterrupt):
                host_module._sandbox_capability_report(pinned, "default")
        self.assertFalse(
            any("rm" in call.args[0] for call in interrupted_run.call_args_list)
        )

    def test_executor_fixture_binds_the_same_pinned_image_and_security_flags(self):
        from tools.p7_controlled_host import (
            P7ControlledHost,
            RunLayout,
            _executor_image_from_fixtures,
        )

        pinned = "python@sha256:" + "a" * 64
        with tempfile.TemporaryDirectory(prefix="p7-executor-fixture-") as temp:
            docker_cli = self._fixture_docker_cli(Path(temp))
            layout = RunLayout.create(Path(temp))
            host = P7ControlledHost(repo_root=Path.cwd(), run_root=Path(temp))
            host._write_fixture(
                layout,
                image_ref=pinned,
                context_name="default",
                docker_cli=docker_cli,
            )
            config = json.loads((layout.fixtures / "executor.json").read_text(encoding="utf-8"))
            wrapper = (layout.fixtures / "docker_executor.py").read_text(encoding="utf-8")
            persisted_image = _executor_image_from_fixtures(layout.fixtures)
            py_compile.compile(
                str(layout.fixtures / "docker_executor.py"), doraise=True
            )
            py_compile.compile(str(layout.fixtures / "judge.py"), doraise=True)

        self.assertEqual(config["image"], pinned)
        self.assertEqual(config["context"], "default")
        self.assertRegex(config["wrapper_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(config["judge_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(persisted_image, pinned)
        for required in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory-swap=256m",
            "--log-driver=none",
            "--user=65534:65534",
            "--init",
        ):
            self.assertIn(required, wrapper)
        self.assertIn('"create"', wrapper)
        self.assertIn('"start", "--attach"', wrapper)
        self.assertIn('"container", "rm", "--force"', wrapper)
        self.assertIn('"ls"', wrapper)
        self.assertIn("drain_error", wrapper)
        self.assertIn("brain-memory.p7.nonce", wrapper)
        self.assertIn("--name=", wrapper)
        self.assertIn("cleanup_verified", wrapper)
        self.assertIn("candidate_target_sha256", wrapper)
        self.assertIn("O_NOFOLLOW", wrapper)
        self.assertIn("owner_id", wrapper)
        self.assertIn("FULL_ID_RE", wrapper)
        self.assertIn("def cleanup_boundary_proven", wrapper)
        self.assertIn("def endpoint_matches", wrapper)
        self.assertIn("def abort_setup", wrapper)
        self.assertIn("cleanup_error", wrapper)
        self.assertIn("endpoint_digest", wrapper)
        self.assertIn("except BaseException", wrapper)
        self.assertIn("post2_code", wrapper)
        self.assertIn('selector = "label=brain-memory.p7.nonce=" + ownership_nonce', wrapper)
        self.assertIn("except ProcessLookupError", wrapper)
        self.assertNotIn("cleanup_reference = ownership_target", wrapper)

    def test_replay_evidence_is_parsed_from_the_fixture_owned_sandbox_runner(self):
        import tools.p7_controlled_host as host_module

        observations = [
            {
                "seed": seed,
                "description_digest": ("a" if index < 4 else "b") * 64,
                "description_count": 1,
            }
            for index, seed in enumerate(host_module.DEFAULT_SEEDS)
        ]
        payload = {
            "status": "fail",
            "verified": True,
            "metrics": {
                "deterministic_replay": 0.0,
                "unique_description_count": 2.0,
            },
            "observations": observations,
        }
        completed = mock.Mock(
            returncode=0,
            stdout=(json.dumps(payload) + "\n").encode("utf-8"),
            stderr=b"",
        )
        with tempfile.TemporaryDirectory(prefix="p7-sandbox-replay-") as temp:
            root = Path(temp)
            active = root / "active"
            fixtures = root / "fixtures"
            (active / "brain").mkdir(parents=True)
            fixtures.mkdir()
            (active / host_module.TARGET_SCOPE).write_text("# target\n", encoding="utf-8")
            (fixtures / "docker_executor.py").write_text("# fixture\n", encoding="utf-8")
            with mock.patch.object(
                host_module,
                "_run_bounded_command",
                return_value=self._bounded_result(completed),
            ) as run:
                result = host_module._sandbox_replay_probe(active, fixtures)

        self.assertEqual(result["executor"], "docker")
        self.assertEqual(result["unique_description_count"], 2)
        self.assertFalse(result["replayable"])
        command = run.call_args.args[0]
        self.assertTrue(any(str(item).endswith("docker_executor.py") for item in command))
        self.assertIn("-I", command)
        self.assertNotIn("-c", command)

    def test_sandbox_replay_binds_the_verified_container_contract(self):
        import tools.p7_controlled_host as host_module

        nonce = "e" * 32
        pinned = "python@sha256:" + "a" * 64
        sandbox = {
            "contract_digest": "b" * 64,
            "container_config_digest": "c" * 64,
            "image_id_digest": "d" * 64,
            "endpoint_digest": hashlib.sha256(b"npipe://local").hexdigest(),
        }
        observations = [
            {
                "seed": seed,
                "description_digest": "f" * 64,
                "description_count": 1,
            }
            for seed in host_module.DEFAULT_SEEDS
        ]
        with tempfile.TemporaryDirectory(prefix="p7-bound-replay-") as temp:
            root = Path(temp)
            docker_cli = self._fixture_docker_cli(root)
            active = root / "active"
            (active / "brain").mkdir(parents=True)
            (active / host_module.TARGET_SCOPE).write_text(
                "# target\n", encoding="utf-8"
            )
            layout = host_module.RunLayout.create(root)
            host = host_module.P7ControlledHost(
                repo_root=Path.cwd(), run_root=root
            )
            host._write_fixture(
                layout,
                image_ref=pinned,
                context_name="default",
                docker_cli=docker_cli,
            )
            config = json.loads(
                (layout.fixtures / "executor.json").read_text(encoding="utf-8")
            )
            config_digest = hashlib.sha256(
                (layout.fixtures / "executor.json").read_bytes()
            ).hexdigest()
            target_digest = hashlib.sha256(
                (active / host_module.TARGET_SCOPE).read_bytes()
            ).hexdigest()
            request = {
                "schema_version": host_module.HOST_SCHEMA_VERSION,
                "protocol": host_module._SANDBOX_PROTOCOL,
                "nonce": nonce,
                "candidate_target_sha256": target_digest,
                "wrapper_sha256": config["wrapper_sha256"],
                "judge_sha256": config["judge_sha256"],
                "executor_config_sha256": config_digest,
                "sandbox_contract_digest": sandbox["contract_digest"],
                "container_config_digest": sandbox["container_config_digest"],
                "image_id_digest": sandbox["image_id_digest"],
                "endpoint_digest": sandbox["endpoint_digest"],
            }
            metadata = {
                **request,
                "request_digest": hashlib.sha256(
                    json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "inspect_verified": True,
                "cleanup_verified": True,
                "input_integrity_verified": True,
                "candidate_target_sha256_before": target_digest,
                "candidate_target_sha256_after": target_digest,
                "judge_sha256_before": config["judge_sha256"],
                "judge_sha256_after": config["judge_sha256"],
            }
            payload = {
                "status": "pass",
                "verified": True,
                "metrics": {
                    "deterministic_replay": 1.0,
                    "unique_description_count": 1.0,
                },
                "observations": observations,
                "metadata": metadata,
            }
            completed = mock.Mock(
                returncode=0,
                stdout=(json.dumps(payload) + "\n").encode("utf-8"),
                stderr=b"",
            )
            with mock.patch.object(
                host_module.secrets, "token_hex", return_value=nonce
            ), mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                return_value=self._bounded_result(completed),
            ):
                result = host_module._sandbox_replay_probe(
                    active, layout.fixtures, sandbox=sandbox
                )

        self.assertTrue(result["replayable"])
        self.assertTrue(result["goal_generation_integrity"])
        self.assertEqual(
            result["execution_metadata"]["sandbox_contract_digest"],
            sandbox["contract_digest"],
        )

    def test_post_swap_replay_summary_rejects_a_behavior_change(self):
        from tools.p7_controlled_host import P7ControlledHost

        execution = {
            "inspect_verified": True,
            "cleanup_verified": True,
            "input_integrity_verified": True,
            **{
                key: "a" * 64
                for key in (
                    "request_digest",
                    "wrapper_sha256",
                    "judge_sha256",
                    "executor_config_sha256",
                    "container_config_digest",
                    "image_id_digest",
                    "endpoint_digest",
                    "sandbox_contract_digest",
                    "candidate_target_sha256_before",
                    "candidate_target_sha256_after",
                    "judge_sha256_before",
                    "judge_sha256_after",
                )
            },
        }
        probe = {
            "replayable": True,
            "goal_generation_integrity": True,
            "unique_description_count": 1,
            "observation_digest": "b" * 64,
            "execution_metadata": execution,
        }
        summary = P7ControlledHost._verified_replay_summary(
            probe, expected_replayable=True, expected_unique=1
        )
        self.assertTrue(summary["verified"])
        with self.assertRaisesRegex(RuntimeError, "post-swap"):
            P7ControlledHost._verified_replay_summary(
                probe,
                expected_replayable=False,
                expected_unique=2,
                expected_observation_digest="c" * 64,
            )

    def test_sandbox_capability_reports_daemon_failure_without_stderr(self):
        import tools.p7_controlled_host as host_module

        completed = mock.Mock(returncode=1)
        completed.stdout = b""
        completed.stderr = b"failed at C:\\Users\\private\\secret\n"
        with mock.patch.object(host_module, "_configured_docker_cli", return_value=sys.executable), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value="npipe://local"
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            return_value=self._bounded_result(completed),
        ):
            report = host_module._sandbox_capability_report(None, "default")
        self.assertFalse(report["ready"])
        self.assertFalse(report["daemon_reachable"])
        self.assertNotIn("private", json.dumps(report))

    def test_sandbox_probe_timeout_cleans_only_its_owned_container_name(self):
        import tools.p7_controlled_host as host_module

        pinned = "python@sha256:" + "a" * 64
        nonce = "d" * 32
        image_id = "sha256:" + "b" * 64
        version = mock.Mock(returncode=0, stdout=b"29.0.0\n", stderr=b"")
        inspected = mock.Mock(
            returncode=0,
            stdout=(
                image_id + "|" + json.dumps(["python@sha256:" + "a" * 64])
            ).encode("utf-8"),
            stderr=b"",
        )
        expected_name = "brain-memory-p7-probe-" + nonce
        owner = mock.Mock(
            returncode=0,
            stdout=(
                "c" * 64
                + "|/"
                + expected_name
                + "|"
                + pinned
                + "|"
                + nonce
                + "\n"
            ).encode("utf-8"),
            stderr=b"",
        )
        removed = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        absent = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        created = mock.Mock(
            returncode=0,
            stdout=("c" * 64 + "\n").encode("utf-8"),
            stderr=b"",
        )
        container_inspected = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "Id": "c" * 64,
                    "Name": "/" + expected_name,
                    "Image": image_id,
                    "Config": {
                        "Image": image_id,
                        "User": "65534:65534",
                        "Labels": {"brain-memory.p7.nonce": nonce},
                    },
                    "HostConfig": {
                        "NetworkMode": "none",
                        "IpcMode": "none",
                        "ReadonlyRootfs": True,
                        "CapDrop": ["ALL"],
                        "SecurityOpt": ["no-new-privileges:true"],
                        "PidsLimit": 64,
                        "Memory": 256 * 1024 * 1024,
                        "MemorySwap": 256 * 1024 * 1024,
                        "NanoCpus": 1_000_000_000,
                        "LogConfig": {"Type": "none", "Config": {}},
                        "ShmSize": 16 * 1024 * 1024,
                        "Init": True,
                        "Ulimits": [
                            {"Name": "nofile", "Soft": 256, "Hard": 256},
                            {"Name": "nproc", "Soft": 64, "Hard": 64},
                        ],
                        "Tmpfs": {
                            "/tmp": "rw,noexec,nosuid,nodev,size=33554432,mode=1777"
                        },
                    },
                    "Mounts": [{"Destination": "/probe", "Type": "bind", "RW": False}],
                }
            ).encode("utf-8"),
            stderr=b"",
        )
        timeout = host_module.subprocess.TimeoutExpired(["docker", "create"], 10)
        with mock.patch.object(
            host_module, "_configured_docker_cli", return_value=sys.executable
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value="npipe://local"
        ), mock.patch.object(
            host_module.secrets, "token_hex", return_value=nonce
        ), mock.patch.object(
            host_module,
            "_run_bounded_command",
            side_effect=[
                self._bounded_result(value)
                for value in (version, inspected, timeout, owner, removed, absent, absent)
            ],
        ) as run:
            report = host_module._sandbox_capability_report(pinned, "default")

        self.assertFalse(report["ready"])
        self.assertEqual(report["reason"], "sandbox_probe_failed")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(
            any(
                "--label=brain-memory.p7.nonce=" + nonce in command
                for command in commands
            )
        )
        # An exception does not carry a closed/drained process boundary.  An
        # ownership lookup may observe the container, but destructive cleanup
        # must remain blocked until a later authenticated startup sweep.
        remove_commands = [command for command in commands if "rm" in command]
        self.assertFalse(remove_commands)

        # Even when a timed-out command happens to report a drained pipe, the
        # same invocation must not perform a destructive cleanup.  The durable
        # lease/reaper handles that terminal state on a later authenticated
        # sweep.
        for uncertain in (
            host_module._BoundedCommandResult(-9, b"", b"", False, True, True),
            host_module._BoundedCommandResult(-9, b"", b"", True, False, True),
        ):
            with mock.patch.object(
                host_module, "_configured_docker_cli", return_value=sys.executable
            ), mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module.secrets, "token_hex", return_value=nonce
            ), mock.patch.object(
                host_module, "_inspect_mount_contract", return_value=True
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                side_effect=[
                    self._bounded_result(version),
                    self._bounded_result(inspected),
                    self._bounded_result(created),
                    self._bounded_result(container_inspected),
                    uncertain,
                ],
            ) as uncertain_run:
                uncertain_report = host_module._sandbox_capability_report(
                    pinned, "default"
                )
            self.assertFalse(uncertain_report["ready"])
            self.assertFalse(
                any("rm" in call.args[0] for call in uncertain_run.call_args_list)
            )

    def test_inspect_mount_contract_rejects_extra_mounts_and_privilege_flags(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-mount-contract-") as temp:
            root = Path(temp)
            probe = root / "probe"
            probe.mkdir()
            base_host = {
                "Binds": [str(probe) + ":/probe:ro"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=32m,mode=1777"},
                "Privileged": False,
                "CapAdd": [], "Devices": [], "DeviceRequests": [],
                "VolumesFrom": [], "Links": [], "PortBindings": {},
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "AutoRemove": False, "RestartPolicy": {},
                "PidMode": "", "UTSMode": "", "UsernsMode": "", "CgroupnsMode": "private",
            }
            mounts = [{"Destination": "/probe", "Type": "bind", "Source": str(probe), "RW": False}]
            self.assertTrue(
                host_module._inspect_mount_contract(mounts, base_host, {"/probe": probe})
            )
            wrong_source = [{**mounts[0], "Source": str(root)}]
            self.assertFalse(
                host_module._inspect_mount_contract(
                    wrong_source, base_host, {"/probe": probe}
                )
            )
            extra = mounts + [{"Destination": "/evil", "Type": "bind", "Source": str(probe), "RW": False}]
            self.assertFalse(
                host_module._inspect_mount_contract(extra, base_host, {"/probe": probe})
            )
            privileged = {**base_host, "Privileged": True}
            self.assertFalse(
                host_module._inspect_mount_contract(mounts, privileged, {"/probe": probe})
            )

    def test_reaper_removes_only_a_uniquely_owned_eval_container(self):
        import tools.p7_controlled_host as host_module

        nonce = "a" * 32
        image = "python@sha256:" + "b" * 64
        with tempfile.TemporaryDirectory(prefix="p7-reaper-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            name = "brain-memory-p7-eval-" + nonce
            container_id = "c" * 64
            listed = mock.Mock(
                returncode=0,
                stdout=(container_id + "|" + name + "\n").encode("utf-8"),
            )
            inspected = mock.Mock(
                returncode=0,
                stdout=(
                    json.dumps(
                        {
                            "Id": container_id,
                            "Name": "/" + name,
                            "Config": {
                                "Image": image,
                                "Labels": {"brain-memory.p7.nonce": nonce},
                            },
                        }
                    )
                    .encode("utf-8")
                ),
            )
            empty = mock.Mock(returncode=0, stdout=b"")
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                side_effect=[
                    self._bounded_result(listed),
                    self._bounded_result(inspected),
                    self._bounded_result(empty),
                    self._bounded_result(empty),
                    self._bounded_result(empty),
                ],
            ) as run:
                self.assertTrue(
                    host_module._reap_owned_eval_container(
                        docker, "default", nonce, image
                    )
                )

        remove_command = run.call_args_list[2].args[0]
        self.assertIn("rm", remove_command)
        self.assertIn(container_id, remove_command)
        self.assertNotIn(name, remove_command[-1:])

    def test_reaper_fails_closed_on_wrong_owner_or_uncertain_query(self):
        import tools.p7_controlled_host as host_module

        nonce = "d" * 32
        image = "python@sha256:" + "e" * 64
        with tempfile.TemporaryDirectory(prefix="p7-reaper-reject-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            name = "brain-memory-p7-eval-" + nonce
            listed = mock.Mock(
                returncode=0,
                stdout=("f" * 64 + "|" + name + "\n").encode("utf-8"),
            )
            wrong_owner = mock.Mock(
                returncode=0,
                stdout=(
                    json.dumps(
                        {
                            "Id": "f" * 64,
                            "Name": "/" + name,
                            "Config": {
                                "Image": image,
                                "Labels": {"brain-memory.p7.nonce": "0" * 32},
                            },
                        }
                    )
                    .encode("utf-8")
                ),
            )
            owned = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "f" * 64,
                        "Name": "/" + name,
                        "Config": {
                            "Image": image,
                            "Labels": {"brain-memory.p7.nonce": nonce},
                        },
                    }
                ).encode("utf-8"),
            )
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                side_effect=[
                    self._bounded_result(listed),
                    self._bounded_result(wrong_owner),
                ],
            ) as run:
                self.assertFalse(
                    host_module._reap_owned_eval_container(
                        docker, "default", nonce, image
                    )
                )
            self.assertEqual(run.call_count, 2)
            self.assertNotIn("rm", run.call_args_list[1].args[0])

            uncertain = host_module._BoundedCommandResult(
                0, b"", b"", False, True, True
            )
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module, "_run_bounded_command", return_value=uncertain
            ) as uncertain_run:
                self.assertFalse(
                    host_module._reap_owned_eval_container(
                        docker, "default", nonce, image
                    )
                )
            self.assertEqual(uncertain_run.call_count, 1)

            # A context switch after ownership inspection must stop before rm.
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                side_effect=[
                    "npipe://local",
                    "npipe://local",
                    "npipe://local",
                    "npipe://other",
                ],
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                side_effect=[
                    self._bounded_result(listed),
                    self._bounded_result(owned),
                ],
            ) as switched_run:
                self.assertFalse(
                    host_module._reap_owned_eval_container(
                        docker,
                        "default",
                        nonce,
                        image,
                        expected_endpoint_digest=host_module._digest(
                            "npipe://local"
                        ),
                    )
                )
            self.assertFalse(
                any("rm" in call.args[0] for call in switched_run.call_args_list)
            )

    def test_reaper_readonly_mode_never_inspects_or_removes_nonempty_target(self):
        import tools.p7_controlled_host as host_module

        nonce = "3" * 32
        image = "python@sha256:" + "4" * 64
        with tempfile.TemporaryDirectory(prefix="p7-reaper-readonly-") as temp:
            docker = self._fixture_docker_cli(Path(temp))
            name = "brain-memory-p7-eval-" + nonce
            listed = mock.Mock(
                returncode=0,
                stdout=("a" * 64 + "|" + name + "\n").encode("utf-8"),
            )
            empty = mock.Mock(returncode=0, stdout=b"")
            with mock.patch.object(
                host_module, "_local_docker_context_endpoint", return_value="npipe://local"
            ), mock.patch.object(
                host_module, "_run_bounded_command", return_value=self._bounded_result(listed)
            ) as nonempty_run:
                self.assertFalse(
                    host_module._reap_owned_eval_container(
                        docker, "default", nonce, image,
                        expected_endpoint_digest=host_module._digest("npipe://local"),
                        allow_removal=False,
                    )
                )
            self.assertEqual(nonempty_run.call_count, 1)
            self.assertNotIn("inspect", nonempty_run.call_args.args[0])
            self.assertNotIn("rm", nonempty_run.call_args.args[0])

            with mock.patch.object(
                host_module, "_local_docker_context_endpoint", return_value="npipe://local"
            ), mock.patch.object(
                host_module, "_run_bounded_command", return_value=self._bounded_result(empty)
            ) as empty_run:
                self.assertTrue(
                    host_module._reap_owned_eval_container(
                        docker, "default", nonce, image,
                        expected_endpoint_digest=host_module._digest("npipe://local"),
                        allow_removal=False,
                    )
                )
            self.assertEqual(empty_run.call_count, 2)

    def test_sandbox_replay_timeout_attempts_nonce_scoped_reaper(self):
        import tools.p7_controlled_host as host_module

        nonce = "1" * 32
        pinned = "python@sha256:" + "2" * 64
        timeout = host_module._BoundedCommandResult(
            -9, b"", b"", False, True, True
        )
        empty = host_module._BoundedCommandResult(0, b"", b"", False, False, True)
        with tempfile.TemporaryDirectory(prefix="p7-replay-timeout-") as temp:
            root = Path(temp)
            active = root / "active"
            (active / "brain").mkdir(parents=True)
            (active / host_module.TARGET_SCOPE).write_text("# target\n", encoding="utf-8")
            layout = host_module.RunLayout.create(root)
            host = host_module.P7ControlledHost(repo_root=Path.cwd(), run_root=root)
            host._write_fixture(
                layout,
                image_ref=pinned,
                context_name="default",
                docker_cli=self._fixture_docker_cli(root),
            )
            with mock.patch.object(
                host_module.secrets, "token_hex", return_value=nonce
            ), mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                side_effect=[timeout, empty, empty],
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "executor failed"):
                    host_module._sandbox_replay_probe(active, layout.fixtures)
            self.assertEqual(run.call_count, 3)
            reaper_command = run.call_args_list[1].args[0]
            self.assertIn("label=brain-memory.p7.nonce=" + nonce, reaper_command)

            # A non-drained result does not prove that the wrapper tree has
            # stopped; the failure path must stay fail-closed and avoid racing
            # a live creator with a cleanup query.
            uncertain = host_module._BoundedCommandResult(
                -9, b"", b"", False, True, False
            )
            with mock.patch.object(
                host_module.secrets, "token_hex", return_value=nonce
            ), mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value="npipe://local",
            ), mock.patch.object(
                host_module,
                "_run_bounded_command",
                return_value=uncertain,
            ) as uncertain_run:
                with self.assertRaisesRegex(RuntimeError, "executor failed"):
                    host_module._sandbox_replay_probe(active, layout.fixtures)
            self.assertEqual(uncertain_run.call_count, 1)
            self.assertNotIn(
                "label=brain-memory.p7.nonce=",
                uncertain_run.call_args_list[0].args[0],
            )

    def test_orphan_lease_registry_reaps_expired_records_and_keeps_tombstone(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "a" * 64
        nonce = "b" * 32
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-lease-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(
                root, "k" * 32, persistent=True
            )
            lease = store.arm(
                operation="eval",
                nonce=nonce,
                name="brain-memory-p7-eval-" + nonce,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            self.assertTrue(
                store.mark_cleanup_pending(
                    lease, now=100, process_stopped=True
                )
            )
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value=endpoint,
            ), mock.patch.object(
                host_module,
                "_reap_owned_eval_container",
                return_value=True,
            ) as reaper:
                status = store.sweep(now=200)
            self.assertTrue(status["ready"])
            self.assertEqual(status["reaped_count"], 1)
            reaper.assert_called_once()
            self.assertTrue(lease.path.is_file())

            cleaned = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["state"], "cleaned")
            self.assertNotIn(str(root), json.dumps(status))

            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value=endpoint,
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=True
            ) as second_reaper:
                second = store.sweep(now=201)
            self.assertTrue(second["ready"])
            second_reaper.assert_called_once()
            self.assertTrue(lease.path.is_file())

    def test_orphan_inspect_is_four_state_read_only_aggregate(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "a" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-inspect-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "i" * 32, persistent=True)
            base_now = int(time.time())

            seed = store.arm(
                operation="eval", nonce="1" * 32,
                name="brain-memory-p7-eval-" + "1" * 32,
                image=image, context="default", docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=base_now, ttl_sec=180,
            )
            seed_payload = json.loads(seed.path.read_text(encoding="utf-8"))
            def write_signed(nonce, **changes):
                payload = dict(seed_payload)
                payload.update(changes, nonce=nonce, name="brain-memory-p7-eval-" + nonce)
                payload["mac"] = store._mac({key: value for key, value in payload.items() if key != "mac"})
                (store.directory / (nonce + ".json")).write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            write_signed("2" * 32, expires_at=base_now + 30)
            write_signed(
                "3" * 32, expires_at=base_now + 30,
                state="cleanup_pending", process_stopped=True,
            )
            write_signed(
                "4" * 32, expires_at=base_now + 30,
                state="cleaned", process_stopped=True, cleaned_at=base_now + 1,
            )

            registry = store.directory
            before_names = sorted(path.name for path in registry.iterdir())
            before_bytes = {
                path.name: path.read_bytes()
                for path in registry.iterdir()
                if path.is_file()
            }
            with mock.patch.object(host_module, "_authorization_file_lock", side_effect=AssertionError("inspect locked")), \
                mock.patch.object(store, "sweep", side_effect=AssertionError("inspect swept")), \
                mock.patch.object(host_module, "_reap_owned_eval_container", side_effect=AssertionError("inspect reaped")), \
                mock.patch.object(store, "_write_transition", side_effect=AssertionError("inspect transitioned")), \
                mock.patch.object(host_module, "_write_json", side_effect=AssertionError("inspect wrote")):
                result = store.inspect(now=base_now + 100)

            self.assertEqual(
                result,
                {
                    "inspection_verified": True,
                    "reason": "",
                    "record_count": 4,
                    "live_waiting_count": 1,
                    "expired_stop_unverified_count": 1,
                    "stopped_pending_cleanup_count": 1,
                    "cleaned_retained_count": 1,
                },
            )
            for key in (
                "record_count", "live_waiting_count", "expired_stop_unverified_count",
                "stopped_pending_cleanup_count", "cleaned_retained_count",
            ):
                self.assertIs(type(result[key]), int)
            self.assertEqual(before_names, sorted(path.name for path in registry.iterdir()))
            self.assertEqual(before_bytes, {
                path.name: path.read_bytes()
                for path in registry.iterdir()
                if path.is_file()
            })
            self.assertIsInstance(result["live_waiting_count"], int)
            self.assertIsNot(result["live_waiting_count"], True)

    def test_orphan_inspect_fail_closed_without_creating_registry(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-orphan-inspect-empty-") as temp:
            root = Path(temp)
            store = host_module.OrphanLeaseStore(root, "j" * 32, persistent=True)
            self.assertFalse(store.directory.exists())
            result = store.inspect()
            self.assertEqual(
                result,
                {
                    "inspection_verified": True,
                    "reason": "",
                    "record_count": 0,
                    "live_waiting_count": 0,
                    "expired_stop_unverified_count": 0,
                    "stopped_pending_cleanup_count": 0,
                    "cleaned_retained_count": 0,
                },
            )
            self.assertFalse(store.directory.exists())

            transient = host_module.OrphanLeaseStore(root, "j" * 32, persistent=False)
            self.assertEqual(
                transient.inspect(),
                {
                    "inspection_verified": False,
                    "reason": "orphan_lease_key_unavailable",
                },
            )
            for bad_now in (True, "200", -1):
                with self.subTest(now=bad_now):
                    self.assertEqual(
                        store.inspect(now=bad_now),
                        {
                            "inspection_verified": False,
                            "reason": "orphan_lease_registry_invalid",
                        },
                    )

            limited_root = root / "limited"
            limited_root.mkdir()
            limited_store = host_module.OrphanLeaseStore(
                limited_root, "j" * 32, persistent=True
            )
            limited_store.directory.mkdir(mode=0o700)
            for index in range(host_module._ORPHAN_LEASE_MAX_FILES + 2):
                (limited_store.directory / (f"{index:064x}.json")).write_text(
                    "{}", encoding="utf-8"
                )
            with mock.patch.object(
                limited_store, "_read_record", side_effect=AssertionError("read past bound")
            ) as read_record:
                self.assertEqual(
                    limited_store.inspect(),
                    {
                        "inspection_verified": False,
                        "reason": "orphan_lease_registry_invalid",
                    },
                )
            read_record.assert_not_called()

    def test_orphan_inspect_grace_boundary_and_tamper_are_fail_closed(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "b" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-inspect-boundary-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "g" * 32, persistent=True)
            lease = store.arm(
                operation="eval",
                nonce="5" * 32,
                name="brain-memory-p7-eval-" + "5" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            self.assertEqual(store.inspect(now=190)["live_waiting_count"], 1)
            self.assertEqual(store.inspect(now=191)["expired_stop_unverified_count"], 1)

            original = json.loads(lease.path.read_text(encoding="utf-8"))
            raw = dict(original)
            raw["mac"] = "0" * 64
            lease.path.write_text(json.dumps(raw), encoding="utf-8")
            result = store.inspect(now=190)
            self.assertEqual(
                result,
                {
                    "inspection_verified": False,
                    "reason": "orphan_lease_registry_invalid",
                },
            )
            lease.path.write_text(json.dumps(original), encoding="utf-8")
            (store.directory / "unexpected.txt").write_text("x", encoding="utf-8")
            self.assertEqual(
                store.inspect(now=190),
                {
                    "inspection_verified": False,
                    "reason": "orphan_lease_registry_invalid",
                },
            )

    def test_orphan_recovery_checkpoint_preserves_lease_and_blocks_same_boot(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "c" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "r" * 32, persistent=True)
            lease = store.arm(
                operation="eval",
                nonce="6" * 32,
                name="brain-memory-p7-eval-" + "6" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            original_lease = lease.path.read_bytes()
            with mock.patch.object(
                host_module, "_read_host_epoch", return_value={
                    "provider": "windows-kuser-v1",
                    "machine_sha256": "a" * 64,
                    "boot_id": 7,
                    "hiberboot": True,
                    "unbiased_interrupt_100ns": 10_000_000_000,
                }
            ):
                prepared = store.prepare_recovery(now=200)
            self.assertEqual(prepared["phase"], "recovery_pending")
            self.assertEqual(prepared["reason"], "orphan_recovery_reboot_required")
            self.assertEqual(prepared["affected_count"], 1)
            recovery_id = prepared["recovery_id"]
            self.assertRegex(recovery_id, r"^[0-9a-f]{32}$")
            self.assertEqual(lease.path.read_bytes(), original_lease)
            self.assertTrue(store.recovery_path.is_file())

            with mock.patch.object(
                host_module, "_read_host_epoch", return_value={
                    "provider": "windows-kuser-v1",
                    "machine_sha256": "a" * 64,
                    "boot_id": 7,
                    "hiberboot": False,
                    "unbiased_interrupt_100ns": 10_000_000_000,
                }
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", side_effect=AssertionError("same boot must not reap")
            ):
                rejected = store.verify_recovery(recovery_id)
            self.assertEqual(rejected["phase"], "blocked")
            self.assertEqual(rejected["reason"], "orphan_recovery_reboot_required")
            self.assertEqual(lease.path.read_bytes(), original_lease)

    def test_host_epoch_bootflags_uses_offset_24_not_padding(self):
        import struct
        import tools.p7_controlled_host as host_module

        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        class FakeKernel32:
            def __init__(self):
                self.boot_id = 9
                self.ReadProcessMemory = FakeFunction(self.read_memory)
                self.GetCurrentProcess = FakeFunction(lambda: 1)
                self.QueryUnbiasedInterruptTime = FakeFunction(self.query_unbiased)

            def read_memory(self, _process, _address, value_ptr, _size, copied_ptr):
                ctypes = host_module.ctypes
                ctypes.cast(value_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = self.boot_id
                ctypes.cast(copied_ptr, ctypes.POINTER(ctypes.c_size_t)).contents.value = 4
                return 1

            def query_unbiased(self, value_ptr):
                ctypes = host_module.ctypes
                ctypes.cast(value_ptr, ctypes.POINTER(ctypes.c_ulonglong)).contents.value = 10_000_000_000
                return 1

        class FakeNtdll:
            def __init__(self, padding, boot_flags):
                self.padding = padding
                self.boot_flags = boot_flags
                self.NtQuerySystemInformation = FakeFunction(self.query)

            def query(self, _class, info_ptr, size, returned_ptr):
                ctypes = host_module.ctypes
                payload = struct.pack("<16sIIQ", b"boot-identifier-1", 1, self.padding, self.boot_flags)
                ctypes.memmove(info_ptr, payload, size)
                ctypes.cast(returned_ptr, ctypes.POINTER(ctypes.c_ulong)).contents.value = 32
                return 0

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_winreg = SimpleNamespace(
            HKEY_LOCAL_MACHINE=1,
            KEY_READ=2,
            KEY_WOW64_64KEY=4,
            OpenKey=lambda *_args: FakeKey(),
            QueryValueEx=lambda *_args: ("12345678-1234-1234-1234-123456789abc", 1),
        )
        import ctypes
        for padding, boot_flags, expected in ((0x2, 0, False), (0, 0x2, True)):
            with self.subTest(padding=padding, boot_flags=boot_flags):
                kernel = FakeKernel32()
                ntdll = FakeNtdll(padding, boot_flags)
                def fake_windll(name, **_kwargs):
                    return kernel if name == "kernel32" else ntdll
                payload = struct.pack("<16sIIQ", b"boot-identifier-1", 1, padding, boot_flags)
                self.assertEqual(len(payload), 32)
                with mock.patch.object(host_module, "os", SimpleNamespace(name="nt")), \
                    mock.patch.object(host_module.sys, "platform", "win32"), \
                    mock.patch.object(host_module.sys, "getwindowsversion", return_value=SimpleNamespace(major=10), create=True), \
                    mock.patch.dict(sys.modules, {"winreg": fake_winreg}), \
                    mock.patch.object(host_module.ctypes, "WinDLL", side_effect=fake_windll, create=True):
                    epoch = host_module._read_host_epoch()
                self.assertEqual(epoch["hiberboot"], expected)

    def test_orphan_recovery_new_boot_empty_resources_seals_and_allows_new_arm(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "d" * 64
        endpoint = "npipe://local"
        before = {
            "provider": "windows-kuser-v1", "machine_sha256": "b" * 64,
            "boot_id": 11, "hiberboot": True, "unbiased_interrupt_100ns": 10_000_000_000,
        }
        after = {**before, "boot_id": 12, "hiberboot": False, "unbiased_interrupt_100ns": 100_000_000}
        with tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-success-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "s" * 32, persistent=True)
            lease = store.arm(
                operation="eval", nonce="7" * 32,
                name="brain-memory-p7-eval-" + "7" * 32,
                image=image, context="default", docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint), now=100, ttl_sec=30,
            )
            original = lease.path.read_bytes()
            with mock.patch.object(host_module, "_read_host_epoch", return_value=before):
                prepared = store.prepare_recovery(now=200)
            recovery_id = prepared["recovery_id"]
            with mock.patch.object(
                host_module, "_read_host_epoch", return_value=after
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=True
            ) as reaper:
                verified = store.verify_recovery(recovery_id)
            self.assertEqual(verified["phase"], "recovery_verified")
            self.assertEqual(verified["reason"], "")
            reaper.assert_called_once()
            self.assertEqual(lease.path.read_bytes(), original)
            later = {**after, "boot_id": 13, "unbiased_interrupt_100ns": 50_000_000}
            with mock.patch.object(host_module, "_read_host_epoch", return_value=later), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=True
            ):
                self.assertTrue(store.sweep(now=300)["ready"])
                repeated_after_reboot = store.verify_recovery(recovery_id)
            self.assertEqual(repeated_after_reboot["phase"], "recovery_verified")
            recovery_bytes = store.recovery_path.read_bytes()
            with mock.patch.object(host_module, "_read_host_epoch", return_value=after):
                no_new_targets = store.prepare_recovery(now=300)
            self.assertEqual(no_new_targets["phase"], "no_recovery_needed")
            self.assertEqual(store.recovery_path.read_bytes(), recovery_bytes)

            with mock.patch.object(
                host_module, "_read_host_epoch", return_value=after
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=True
            ) as sweep_reaper:
                new_lease = store.arm(
                    operation="eval", nonce="8" * 32,
                    name="brain-memory-p7-eval-" + "8" * 32,
                    image=image, context="default", docker_cli=docker,
                    endpoint_digest=host_module._digest(endpoint), now=300, ttl_sec=30,
                )
            self.assertTrue(sweep_reaper.called)
            self.assertTrue(all(call.kwargs.get("allow_removal") is False for call in sweep_reaper.call_args_list))
            self.assertTrue(new_lease.path.is_file())
            with mock.patch.object(
                host_module, "_read_host_epoch", return_value=after
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=True
            ):
                repeated = store.verify_recovery(recovery_id)
            self.assertEqual(repeated["phase"], "recovery_verified")

    def test_orphan_recovery_resource_reappearance_stays_unverified(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "e" * 64
        endpoint = "npipe://local"
        before = {
            "provider": "windows-kuser-v1", "machine_sha256": "c" * 64,
            "boot_id": 21, "hiberboot": True, "unbiased_interrupt_100ns": 10_000_000_000,
        }
        after = {**before, "boot_id": 22, "hiberboot": False, "unbiased_interrupt_100ns": 100_000_000}
        with tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-resource-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "t" * 32, persistent=True)
            lease = store.arm(
                operation="eval", nonce="9" * 32,
                name="brain-memory-p7-eval-" + "9" * 32,
                image=image, context="default", docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint), now=100, ttl_sec=30,
            )
            original = lease.path.read_bytes()
            with mock.patch.object(host_module, "_read_host_epoch", return_value=before):
                recovery_id = store.prepare_recovery(now=200)["recovery_id"]
            with mock.patch.object(
                host_module, "_read_host_epoch", return_value=after
            ), mock.patch.object(
                host_module, "_reap_owned_eval_container", return_value=False
            ) as reaper:
                result = store.verify_recovery(recovery_id)
            self.assertEqual(result["phase"], "blocked")
            self.assertEqual(result["reason"], "orphan_recovery_resources_unverified")
            reaper.assert_called_once()
            self.assertEqual(lease.path.read_bytes(), original)

    def test_orphan_recovery_wrong_key_tamper_and_foreign_epoch_fail_closed(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "f" * 64
        endpoint = "npipe://local"
        before = {
            "provider": "windows-kuser-v1", "machine_sha256": "d" * 64,
            "boot_id": 31, "hiberboot": True, "unbiased_interrupt_100ns": 10_000_000_000,
        }
        after = {**before, "boot_id": 32, "hiberboot": False, "unbiased_interrupt_100ns": 100_000_000}

        def make_checkpoint(root, key):
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, key, persistent=True)
            lease = store.arm(
                operation="eval", nonce="a" * 32,
                name="brain-memory-p7-eval-" + "a" * 32,
                image=image, context="default", docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint), now=100, ttl_sec=30,
            )
            with mock.patch.object(host_module, "_read_host_epoch", return_value=before):
                recovery_id = store.prepare_recovery(now=200)["recovery_id"]
            return store, lease, recovery_id

        with tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-negative-") as temp:
            root = Path(temp)
            store, lease, recovery_id = make_checkpoint(root, "w" * 32)
            original = lease.path.read_bytes()

            wrong_key = host_module.OrphanLeaseStore(root, "x" * 32, persistent=True)
            self.assertEqual(
                wrong_key.verify_recovery(recovery_id)["reason"],
                "orphan_recovery_registry_invalid",
            )
            raw = json.loads(store.recovery_path.read_text(encoding="utf-8"))
            raw["entries"][0]["checkpoint"]["targets"] = []
            store.recovery_path.write_text(json.dumps(raw), encoding="utf-8")
            with mock.patch.object(host_module, "_read_host_epoch", return_value=after):
                tampered = store.verify_recovery(recovery_id)
            self.assertEqual(tampered["reason"], "orphan_recovery_registry_invalid")
            self.assertEqual(lease.path.read_bytes(), original)

        for label, epoch in (
            ("foreign", {**after, "machine_sha256": "e" * 64}),
            ("boot_backwards", {**before, "boot_id": 30, "hiberboot": False}),
            ("hiberboot", {**after, "hiberboot": True}),
            ("uptime_not_reset", {**after, "unbiased_interrupt_100ns": 10_000_000_000}),
        ):
            with self.subTest(epoch=label), tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-epoch-") as temp:
                store, lease, recovery_id = make_checkpoint(Path(temp), "y" * 32)
                original = lease.path.read_bytes()
                with mock.patch.object(host_module, "_read_host_epoch", return_value=epoch), mock.patch.object(
                    host_module, "_reap_owned_eval_container", side_effect=AssertionError("epoch rejection must precede Docker")
                ) as reaper:
                    rejected = store.verify_recovery(recovery_id)
                self.assertEqual(rejected["phase"], "blocked")
                self.assertIn(rejected["reason"], {
                    "orphan_recovery_host_mismatch", "orphan_recovery_reboot_required",
                })
                reaper.assert_not_called()
                self.assertEqual(lease.path.read_bytes(), original)

    def test_orphan_recovery_authenticated_snapshot_change_requires_new_checkpoint(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "1" * 64
        endpoint = "npipe://local"
        epoch = {
            "provider": "windows-kuser-v1", "machine_sha256": "f" * 64,
            "boot_id": 41, "hiberboot": True, "unbiased_interrupt_100ns": 10_000_000_000,
        }
        with tempfile.TemporaryDirectory(prefix="p7-orphan-recovery-snapshot-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "z" * 32, persistent=True)
            lease = store.arm(
                operation="eval", nonce="b" * 32,
                name="brain-memory-p7-eval-" + "b" * 32,
                image=image, context="default", docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint), now=100, ttl_sec=30,
            )
            with mock.patch.object(host_module, "_read_host_epoch", return_value=epoch):
                first = store.prepare_recovery(now=200)
            original = json.loads(lease.path.read_text(encoding="utf-8"))
            changed = dict(original, expires_at=original["expires_at"] + 1)
            changed["mac"] = store._mac({key: value for key, value in changed.items() if key != "mac"})
            lease.path.write_text(json.dumps(changed), encoding="utf-8")
            with mock.patch.object(host_module, "_read_host_epoch", return_value=epoch):
                rejected = store.verify_recovery(first["recovery_id"])
                appended = store.prepare_recovery(now=300)
            self.assertEqual(rejected["phase"], "blocked")
            self.assertEqual(rejected["reason"], "orphan_recovery_registry_changed")
            self.assertEqual(appended["phase"], "recovery_pending")
            self.assertNotEqual(appended["recovery_id"], first["recovery_id"])
            with mock.patch.object(host_module, "_read_host_epoch", return_value={**epoch, "boot_id": 42, "hiberboot": False}):
                superseded = store.verify_recovery(first["recovery_id"])
            self.assertEqual(superseded["phase"], "blocked")
            self.assertEqual(superseded["reason"], "orphan_recovery_checkpoint_superseded")

    def test_orphan_recovery_cli_key_errors_are_static_and_do_not_import_brain(self):
        script = (
            "import builtins, runpy\n"
            "real_import = builtins.__import__\n"
            "def guarded(name, *args, **kwargs):\n"
            "    if name == 'brain' or name.startswith('brain.') or name == 'services' or name.startswith('services.'):\n"
            "        raise AssertionError('forbidden import: ' + name)\n"
            "    return real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "try:\n"
            "    runpy.run_path(r'tools/p7_controlled_host.py', run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    raise SystemExit(exc.code)\n"
        )
        for key, expected in ((None, "manifest_key_unavailable"), ("short", "manifest_key_invalid")):
            with self.subTest(key=key):
                isolated = tempfile.mkdtemp(prefix="p7-recovery-cli-")
                env = {
                    name: value for name, value in os.environ.items()
                    if name in {"PATH", "Path", "SystemRoot", "SYSTEMROOT", "PATHEXT"}
                }
                env.update({
                    "OFFLINE": "1", "BRAIN_MEMORY_OFFLINE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "TEMP": isolated, "TMP": isolated,
                })
                if key is not None:
                    env["BRAIN_MEMORY_P7_MANIFEST_KEY"] = key
                completed = subprocess.run(
                    [sys.executable, "-c", script, "--prepare-orphan-recovery"],
                    cwd=Path.cwd(), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=20, check=False,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stderr, "")
                report = json.loads(completed.stdout.strip())
                self.assertEqual(report["phase"], "blocked")
                self.assertTrue(report["evidence_only"])
                self.assertEqual(report["reason"], expected)
                self.assertNotIn("authorization_required", report)

    def test_recovery_sidecar_without_registry_fails_closed_everywhere(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-recovery-sidecar-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "q" * 32, persistent=True)
            store.recovery_path.write_text("{}", encoding="utf-8")
            self.assertFalse(store.directory.exists())
            self.assertIn("registry", store.inspect()["reason"])
            self.assertFalse(store.sweep(now=200)["ready"])
            with self.assertRaisesRegex(RuntimeError, "cleanup is pending"):
                store.arm(
                    operation="eval", nonce="c" * 32,
                    name="brain-memory-p7-eval-" + "c" * 32,
                    image="python@sha256:" + "2" * 64,
                    context="default", docker_cli=docker,
                    endpoint_digest=host_module._digest("npipe://local"),
                    now=100, ttl_sec=30,
                )

    def test_inspect_orphans_cli_requires_own_mode_and_does_not_construct_without_key(self):
        import tools.p7_controlled_host as host_module

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["p7_controlled_host.py", "--inspect-orphans"]
        ), mock.patch.object(host_module, "P7ControlledHost") as host:
            with mock.patch("builtins.print") as output:
                self.assertEqual(host_module._cli(), 2)
            host.assert_not_called()
            report = json.loads(output.call_args.args[0])
            self.assertEqual(report["phase"], "blocked")
            self.assertTrue(report["observation_only"])
            self.assertEqual(report["reason_code"], "manifest_key_unavailable")
            self.assertNotIn("clear", report)
            self.assertNotIn("ready", report)

        with mock.patch.object(
            sys, "argv", ["p7_controlled_host.py", "--inspect-orphans", "--prepare"]
        ):
            with self.assertRaises(SystemExit):
                host_module._cli()

        fake_store = SimpleNamespace(
            inspect=lambda: {
                "inspection_verified": True,
                "reason": "",
                "record_count": 0,
                "live_waiting_count": 0,
                "expired_stop_unverified_count": 0,
                "stopped_pending_cleanup_count": 0,
                "cleaned_retained_count": 0,
            }
        )
        fake_host = SimpleNamespace(
            run_root=Path(tempfile.gettempdir()),
            _orphan_lease_store=lambda: fake_store,
        )
        with mock.patch.dict(
            os.environ, {host_module.MANIFEST_KEY_ENV: "v" * 32}, clear=False
        ), mock.patch.object(
            sys, "argv", ["p7_controlled_host.py", "--inspect-orphans"]
        ), mock.patch.object(
            host_module, "P7ControlledHost", return_value=fake_host
        ), mock.patch.object(
            host_module, "_authorization_file_lock", side_effect=AssertionError("inspect host lock")
        ) as host_lock, mock.patch("builtins.print") as output:
            self.assertEqual(host_module._cli(), 0)
        host_lock.assert_not_called()
        valid_report = json.loads(output.call_args.args[0])
        self.assertEqual(valid_report["phase"], "inspected")
        self.assertTrue(valid_report["observation_only"])
        self.assertNotIn("authorization_required", valid_report)

    def test_inspect_orphans_fresh_process_key_modes_never_imports_brain_or_services(self):
        script = (
            "import builtins, runpy\n"
            "real_import = builtins.__import__\n"
            "def guarded(name, *args, **kwargs):\n"
            "    if name == 'brain' or name.startswith('brain.') or name == 'services' or name.startswith('services.'):\n"
            "        raise AssertionError('diagnostic imported forbidden module: ' + name)\n"
            "    return real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "try:\n"
            "    runpy.run_path(r'tools/p7_controlled_host.py', run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    raise SystemExit(exc.code)\n"
        )
        for label, key, expected_code, expected_phase, expected_reason in (
            ("missing", None, 2, "blocked", "manifest_key_unavailable"),
            ("invalid", "short", 2, "blocked", "manifest_key_invalid"),
            ("valid", "v" * 32, 0, "inspected", None),
        ):
            with self.subTest(key_mode=label):
                isolated_temp = tempfile.mkdtemp(prefix="p7-inspect-cli-")
                child_env = {
                    key_name: value
                    for key_name, value in os.environ.items()
                    if key_name in {"PATH", "Path", "SystemRoot", "SYSTEMROOT", "PATHEXT"}
                }
                child_env.update(
                    {
                        "OFFLINE": "1",
                        "BRAIN_MEMORY_OFFLINE": "1",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "TEMP": isolated_temp,
                        "TMP": isolated_temp,
                    }
                )
                child_env.pop("BRAIN_MEMORY_P7_MANIFEST_KEY", None)
                if key is not None:
                    child_env["BRAIN_MEMORY_P7_MANIFEST_KEY"] = key
                completed = subprocess.run(
                    [sys.executable, "-c", script, "--inspect-orphans"],
                    cwd=Path.cwd(),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                self.assertEqual(completed.stderr, "")
                report = json.loads(completed.stdout.strip())
                self.assertEqual(report["phase"], expected_phase)
                self.assertTrue(report["observation_only"])
                self.assertNotIn("authorization_required", report)
                if expected_reason:
                    self.assertEqual(report["reason_code"], expected_reason)
                else:
                    self.assertNotIn("reason_code", report)
                    self.assertNotIn("ready", report)
                    self.assertNotIn("clear", report)

    def test_orphan_lease_registry_never_reaps_an_unstopped_creator(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "a" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-live-process-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "k" * 32, persistent=True)
            lease = store.arm(
                operation="eval",
                nonce="9" * 32,
                name="brain-memory-p7-eval-" + "9" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            with mock.patch.object(
                host_module, "_reap_owned_eval_container"
            ) as reaper:
                status = store.sweep(now=200)
            self.assertFalse(status["ready"])
            self.assertEqual(status["reason"], "orphan_cleanup_unverified")
            reaper.assert_not_called()
            self.assertTrue(lease.path.is_file())

    def test_orphan_lease_state_is_monotonic_and_completion_requires_pending(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "a" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-state-guards-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "s" * 32, persistent=True)
            lease = store.arm(
                operation="eval",
                nonce="a" * 32,
                name="brain-memory-p7-eval-" + "a" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )

            # An armed lease cannot jump directly to a cleaned tombstone.
            self.assertFalse(store.complete(lease, now=100))
            self.assertEqual(
                json.loads(lease.path.read_text(encoding="utf-8"))["state"],
                "armed",
            )

            self.assertTrue(
                store.mark_cleanup_pending(
                    lease, now=101, process_stopped=True
                )
            )
            pending = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(pending["state"], "cleanup_pending")
            self.assertIs(pending["process_stopped"], True)

            # A later failure/finally path must not downgrade the durable stop
            # fact back to False.
            self.assertFalse(
                store.mark_cleanup_pending(
                    lease, now=102, process_stopped=False
                )
            )
            retained = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(retained["state"], "cleanup_pending")
            self.assertIs(retained["process_stopped"], True)

            self.assertTrue(store.complete(lease, now=103))
            cleaned = json.loads(lease.path.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["state"], "cleaned")
            self.assertIs(cleaned["process_stopped"], True)
            # Tombstones remain safely idempotent.
            self.assertTrue(store.complete(lease, now=104))

    def test_orphan_lease_validator_rejects_armed_stopped_record(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "b" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-state-invalid-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(root, "t" * 32, persistent=True)
            lease = store.arm(
                operation="probe",
                nonce="b" * 32,
                name="brain-memory-p7-probe-" + "b" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            raw = json.loads(lease.path.read_text(encoding="utf-8"))
            raw["process_stopped"] = True
            unsigned = {
                key: value
                for key, value in raw.items()
                if key != "mac"
            }
            raw["mac"] = store._mac(unsigned)
            lease.path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "orphan lease record is invalid"):
                store._read_record(lease.path)

    def test_orphan_lease_registry_is_namespaced_by_project(self):
        import tools.p7_controlled_host as host_module

        with tempfile.TemporaryDirectory(prefix="p7-orphan-namespace-") as temp:
            root = Path(temp)
            project_a = root / "project-a"
            project_b = root / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            first = host_module.OrphanLeaseStore(
                root, "n" * 32, namespace=project_a, persistent=True
            )
            second = host_module.OrphanLeaseStore(
                root, "n" * 32, namespace=project_b, persistent=True
            )
            self.assertNotEqual(first.directory, second.directory)

            transient = host_module.OrphanLeaseStore(
                root, "n" * 32, namespace=project_a, persistent=False
            )
            transient_status = transient.sweep()
            self.assertFalse(transient_status["ready"])
            self.assertEqual(
                transient_status["reason"], "orphan_lease_key_unavailable"
            )
            self.assertFalse(transient.directory.exists())

    def test_orphan_lease_registry_rejects_tampering_and_live_records(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "c" * 64
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-lease-reject-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(
                root, "m" * 32, persistent=True
            )
            tampered = store.arm(
                operation="probe",
                nonce="d" * 32,
                name="brain-memory-p7-probe-" + "d" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            raw = json.loads(tampered.path.read_text(encoding="utf-8"))
            raw["mac"] = "0" * 64
            tampered.path.write_text(json.dumps(raw), encoding="utf-8")
            with mock.patch.object(
                host_module, "_reap_owned_eval_container"
            ) as reaper:
                rejected = store.sweep(now=200)
            self.assertFalse(rejected["ready"])
            self.assertEqual(rejected["reason"], "orphan_lease_invalid")
            reaper.assert_not_called()
            self.assertTrue(tampered.path.exists())

            live_root = root / "live"
            live_root.mkdir()
            live_store = host_module.OrphanLeaseStore(
                live_root, "m" * 32, persistent=True
            )
            live = live_store.arm(
                operation="eval",
                nonce="e" * 32,
                name="brain-memory-p7-eval-" + "e" * 32,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=1000,
            )
            # A fresh process must not touch a lease which may still belong to
            # a live creator, even if another record was already quarantined.
            self.assertTrue(live.path.is_file())

    def test_prepare_sweeps_orphan_before_capability_probe(self):
        import tools.p7_controlled_host as host_module

        image = "python@sha256:" + "f" * 64
        nonce = "1" * 32
        endpoint = "npipe://local"
        with tempfile.TemporaryDirectory(prefix="p7-orphan-startup-") as temp:
            root = Path(temp)
            docker = self._fixture_docker_cli(root)
            store = host_module.OrphanLeaseStore(
                root,
                "p" * 32,
                namespace=Path.cwd(),
                persistent=True,
            )
            lease = store.arm(
                operation="eval",
                nonce=nonce,
                name="brain-memory-p7-eval-" + nonce,
                image=image,
                context="default",
                docker_cli=docker,
                endpoint_digest=host_module._digest(endpoint),
                now=100,
                ttl_sec=30,
            )
            self.assertTrue(
                store.mark_cleanup_pending(
                    lease, now=100, process_stopped=True
                )
            )
            host = host_module.P7ControlledHost(
                repo_root=Path.cwd(), run_root=root, manifest_key="p" * 32
            )
            with mock.patch.object(
                host_module,
                "_local_docker_context_endpoint",
                return_value=endpoint,
            ), mock.patch.object(
                host_module,
                "_reap_owned_eval_container",
                return_value=False,
            ), mock.patch.object(
                host_module, "_sandbox_capability_report"
            ) as capability:
                result = asyncio.run(host.prepare_async())
            self.assertEqual(result["phase"], "blocked")
            self.assertEqual(result["reason"], "orphan_cleanup_unverified")
            capability.assert_not_called()

    def test_prepare_stops_before_runtime_when_sandbox_is_unavailable(self):
        from tools.p7_controlled_host import P7ControlledHost, RunLayout

        with tempfile.TemporaryDirectory(prefix="p7-sandbox-gate-") as temp:
            layout = RunLayout.create(Path(temp))
            host = P7ControlledHost(
                repo_root=Path.cwd(),
                run_root=Path(temp),
                manifest_key="k" * 32,
            )
            with mock.patch(
                "tools.p7_controlled_host._configured_docker_context",
                return_value="default",
            ), mock.patch.object(host, "_prepare_layout", return_value=layout), mock.patch(
                "tools.p7_controlled_host._sandbox_capability_report",
                return_value={
                    "ready": False,
                    "executor": "none",
                    "daemon_reachable": False,
                    "network_isolation_verified": False,
                    "filesystem_isolation_verified": False,
                    "reason": "external_sandbox_unavailable",
                },
            ), mock.patch.object(host, "_build_runtime") as build_runtime:
                result = asyncio.run(host.prepare_async())
            self.assertEqual(result["phase"], "blocked")
            self.assertEqual(result["reason"], "external_sandbox_unavailable")
            build_runtime.assert_not_called()

    def test_formal_iteration_evaluation_is_armed_before_execution_and_sealed(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        nonce = "a" * 32
        events = []
        request = self._formal_request(host_module, endpoint, nonce=nonce)
        request["nested"] = {"proof": {"value": 7}}
        receipt = self._host_evaluation_receipt(
            execution_request=request,
            metadata=self._formal_execution_metadata(host_module, request),
        )
        stem = mock.Mock()

        def evaluate(*args, **kwargs):
            events.append("evaluate")
            kwargs["execution_request"]["nested"]["proof"]["value"] = 99
            return receipt

        stem.evaluate_iteration_proposal.side_effect = evaluate
        runtime = mock.Mock()
        runtime.layout.fixtures = Path("fixture-root")
        runtime.layout.run_id = "run-1"
        runtime.harness = self._formal_harness()
        runtime.stem = stem
        lease = object()
        lease_store = mock.Mock()

        def arm(**kwargs):
            events.append(("arm", kwargs))
            return lease

        def mark(value, **kwargs):
            events.append(("mark", kwargs))
            return True

        def complete(value, **kwargs):
            events.append("complete")
            return True

        lease_store.arm.side_effect = arm
        lease_store.mark_cleanup_pending.side_effect = mark
        lease_store.complete.side_effect = complete
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(),
            run_root=Path.cwd(),
            manifest_key="k" * 32,
        )
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "b" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ):
            result = host._evaluate_iteration_with_lease(
                runtime,
                object(),
                mock.Mock(fingerprint="c" * 64),
                mock.Mock(fingerprint="b" * 64),
                execution_request=request,
                lease_store=lease_store,
            )

        self.assertIs(result, receipt)
        self.assertEqual([item if isinstance(item, str) else item[0] for item in events], ["arm", "evaluate", "mark", "complete"])
        self.assertEqual(events[0][1]["nonce"], nonce)
        self.assertIs(events[2][1]["process_stopped"], True)
        self.assertEqual(request["nested"]["proof"]["value"], 7)
        lease_store.complete.assert_called_once_with(lease)

    def test_formal_iteration_evaluation_failure_leaves_a_nonstopped_lease(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        nonce = "b" * 32
        lease_store = mock.Mock()
        lease_store.arm.return_value = object()
        lease_store.mark_cleanup_pending.side_effect = RuntimeError("cleanup db down")
        runtime = mock.Mock()
        runtime.layout.fixtures = Path("fixture-root")
        runtime.layout.run_id = "run-2"
        runtime.harness = self._formal_harness()
        runtime.stem.evaluate_iteration_proposal.side_effect = RuntimeError("child failed")
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(),
            run_root=Path.cwd(),
            manifest_key="k" * 32,
        )
        request = self._formal_request(host_module, endpoint, nonce=nonce)
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "c" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ), mock.patch.object(host, "_validate_execution_metadata"):
            with self.assertRaisesRegex(RuntimeError, "child failed") as raised:
                host._evaluate_iteration_with_lease(
                    runtime,
                    object(),
                    mock.Mock(fingerprint="c" * 64),
                    mock.Mock(fingerprint="b" * 64),
                    execution_request=request,
                    lease_store=lease_store,
                )

        lease_store.mark_cleanup_pending.assert_called_once()
        self.assertIn(
            "cleanup registration failed",
            raised.exception.__notes__[0],
        )
        self.assertIs(
            lease_store.mark_cleanup_pending.call_args.kwargs["process_stopped"],
            False,
        )
        lease_store.complete.assert_not_called()

    def test_real_execution_metadata_binding_mismatch_never_completes(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        request = self._formal_request(host_module, endpoint, nonce="3" * 32)
        metadata = self._formal_execution_metadata(host_module, request)
        metadata["wrapper_sha256"] = "9" * 64
        receipt = self._host_evaluation_receipt(
            execution_request=request, metadata=metadata
        )
        lease_store = mock.Mock()
        lease_store.arm.return_value = object()
        lease_store.mark_cleanup_pending.return_value = True
        runtime = mock.Mock()
        runtime.layout.fixtures = Path("fixture-root")
        runtime.layout.run_id = "run-proof-mismatch"
        runtime.harness = self._formal_harness()
        runtime.stem.evaluate_iteration_proposal.return_value = receipt
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(), run_root=Path.cwd(), manifest_key="k" * 32
        )
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "e" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture binding changed"):
                host._evaluate_iteration_with_lease(
                    runtime, object(), mock.Mock(fingerprint="c" * 64),
                    mock.Mock(fingerprint="b" * 64), execution_request=request,
                    lease_store=lease_store,
                )
        lease_store.mark_cleanup_pending.assert_called_once_with(
            mock.ANY, process_stopped=True
        )
        lease_store.complete.assert_not_called()

    def test_failed_evaluation_with_closed_boundary_marks_stopped_but_never_completes(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        nonce = "d" * 32
        request = self._formal_request(host_module, endpoint, nonce=nonce)
        receipt = self._host_evaluation_receipt(
            execution_request=request,
            exit_code=1,
            startup_passed=False,
            result_verified=False,
            metadata={},
            error="fixture reported failure",
        )
        lease_store = mock.Mock()
        lease_store.arm.return_value = object()
        lease_store.mark_cleanup_pending.return_value = True
        runtime = mock.Mock()
        runtime.layout.fixtures = Path("fixture-root")
        runtime.layout.run_id = "run-failed-boundary"
        runtime.harness = self._formal_harness()
        runtime.stem.evaluate_iteration_proposal.return_value = receipt
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(),
            run_root=Path.cwd(),
            manifest_key="k" * 32,
        )
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "e" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ), mock.patch.object(host, "_validate_execution_metadata"):
            with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
                host._evaluate_iteration_with_lease(
                    runtime,
                    object(),
                    mock.Mock(fingerprint="c" * 64),
                    mock.Mock(fingerprint="b" * 64),
                    execution_request=request,
                    lease_store=lease_store,
                )
        lease_store.mark_cleanup_pending.assert_called_once_with(
            mock.ANY, process_stopped=True
        )
        lease_store.complete.assert_not_called()

    def test_timeout_closed_boundary_marks_stopped_but_still_rejects_evaluation(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        request = self._formal_request(host_module, endpoint, nonce="e" * 32)
        receipt = self._host_evaluation_receipt(
            execution_request=request, exit_code=-9, timed_out=True,
            startup_passed=False, result_verified=False, metadata={},
        )
        lease_store = mock.Mock()
        lease_store.arm.return_value = object()
        lease_store.mark_cleanup_pending.return_value = True
        runtime = mock.Mock()
        runtime.layout.fixtures = Path("fixture-root")
        runtime.layout.run_id = "run-timeout"
        runtime.harness = self._formal_harness()
        runtime.stem.evaluate_iteration_proposal.return_value = receipt
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(), run_root=Path.cwd(), manifest_key="k" * 32
        )
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "e" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ), mock.patch.object(host, "_validate_execution_metadata"):
            with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
                host._evaluate_iteration_with_lease(
                    runtime, object(), mock.Mock(fingerprint="c" * 64),
                    mock.Mock(fingerprint="b" * 64), execution_request=request,
                    lease_store=lease_store,
                )
        lease_store.mark_cleanup_pending.assert_called_once_with(
            mock.ANY, process_stopped=True
        )
        lease_store.complete.assert_not_called()

    def test_stop_proof_rejects_unclosed_or_non_boolean_timeout_evidence(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(), run_root=Path.cwd(), manifest_key="k" * 32
        )
        for case, receipt_kwargs in (
            ("missing-digest", {"error": "failed"}),
            ("stale-digest", {"execution_request_digest": "0" * 64, "error": "failed"}),
            ("duplicate-startup", {"duplicate_startup": True, "error": "failed"}),
            ("candidate-binding", {"candidate_fingerprint": "9" * 64, "error": "failed"}),
            ("fixture-binding", {"fixture_hash": "9" * 64, "error": "failed"}),
            ("bool-exit-code", {"exit_code": True, "error": "failed"}),
            ("none-exit-code", {"exit_code": None, "error": "failed"}),
            ("unclosed", {"boundary_closed": False, "error": "failed"}),
            ("timeout-unclosed", {"timed_out": True, "boundary_closed": False, "exit_code": -9, "error": "failed"}),
            ("raw-timeout-type", {"exit_code": 1, "startup_timed_out": 1, "error": "failed"}),
            ("timeout-mismatch", {"timed_out": True, "startup_timed_out": False, "exit_code": 1, "error": "failed"}),
        ):
            with self.subTest(case=case):
                nonce = ("f" * 32) if case == "unclosed" else ("1" * 32 if case == "raw-timeout-type" else "2" * 32)
                request = self._formal_request(host_module, endpoint, nonce=nonce)
                receipt = self._host_evaluation_receipt(
                    execution_request=request,
                    startup_passed=False,
                    result_verified=False,
                    metadata={},
                    **{key: value for key, value in receipt_kwargs.items()
                       if key not in {"execution_request_digest", "duplicate_startup"}},
                )
                startup = receipt.gates[0]
                evidence = dict(startup.evidence)
                if "execution_request_digest" in receipt_kwargs:
                    evidence["execution_request_digest"] = receipt_kwargs["execution_request_digest"]
                elif case == "missing-digest":
                    evidence.pop("execution_request_digest", None)
                receipt = replace(receipt, gates=(replace(startup, evidence=evidence),) + receipt.gates[1:])
                if receipt_kwargs.get("duplicate_startup"):
                    receipt = replace(receipt, gates=receipt.gates + (receipt.gates[0],))
                lease_store = mock.Mock()
                lease_store.arm.return_value = object()
                lease_store.mark_cleanup_pending.return_value = True
                runtime = mock.Mock()
                runtime.layout.fixtures = Path("fixture-root")
                runtime.layout.run_id = "run-" + case
                runtime.harness = self._formal_harness()
                runtime.stem.evaluate_iteration_proposal.return_value = receipt
                with mock.patch.object(
                    host_module,
                    "_executor_config_from_fixtures",
                    return_value=("python@sha256:" + "e" * 64, "default", sys.executable),
                ), mock.patch.object(
                    host_module, "_local_docker_context_endpoint", return_value=endpoint
                ), mock.patch.object(host, "_validate_execution_metadata"):
                    with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
                        host._evaluate_iteration_with_lease(
                            runtime, object(), mock.Mock(fingerprint="c" * 64),
                            mock.Mock(fingerprint="b" * 64), execution_request=request,
                            lease_store=lease_store,
                        )
                lease_store.mark_cleanup_pending.assert_called_once_with(
                    mock.ANY, process_stopped=False
                )
                lease_store.complete.assert_not_called()

    def test_formal_iteration_requires_verified_isolated_receipt(self):
        import tools.p7_controlled_host as host_module

        endpoint = "npipe://local"
        request = {
            "nonce": "c" * 32,
            "endpoint_digest": host_module._digest(endpoint),
            "candidate_fingerprint": "c" * 64,
            "baseline_fingerprint": "b" * 64,
            "fixture_hash": "f" * 64,
            "evaluator_hash": "e" * 64,
        }
        invalid_hash = self._host_evaluation_receipt()
        object.__setattr__(invalid_hash, "receipt_hash", "0" * 64)
        cases = (
            (object(), "invalid receipt type"),
            (invalid_hash, "verification failed"),
            (self._host_evaluation_receipt(isolated=False), "not isolated"),
        )
        host = host_module.P7ControlledHost(
            repo_root=Path.cwd(),
            run_root=Path.cwd(),
            manifest_key="k" * 32,
        )
        with mock.patch.object(
            host_module,
            "_executor_config_from_fixtures",
            return_value=("python@sha256:" + "d" * 64, "default", sys.executable),
        ), mock.patch.object(
            host_module, "_local_docker_context_endpoint", return_value=endpoint
        ), mock.patch.object(host, "_validate_execution_metadata"):
            for receipt, reason in cases:
                with self.subTest(reason=reason):
                    lease_store = mock.Mock()
                    lease_store.arm.return_value = object()
                    lease_store.mark_cleanup_pending.return_value = True
                    runtime = mock.Mock()
                    runtime.layout.fixtures = Path("fixture-root")
                    runtime.layout.run_id = "run-3"
                    runtime.harness = self._formal_harness()
                    runtime.stem.evaluate_iteration_proposal.return_value = receipt
                    with self.assertRaisesRegex(RuntimeError, reason):
                        host._evaluate_iteration_with_lease(
                            runtime,
                            object(),
                            mock.Mock(fingerprint="c" * 64),
                            mock.Mock(fingerprint="b" * 64),
                            execution_request=request,
                            lease_store=lease_store,
                        )
                    lease_store.mark_cleanup_pending.assert_called_once_with(
                        mock.ANY, process_stopped=False
                    )
                    lease_store.complete.assert_not_called()

    def test_formal_evaluation_has_one_lease_owned_execution_seam(self):
        import tools.p7_controlled_host as host_module

        source = inspect.getsource(host_module.P7ControlledHost)
        self.assertEqual(source.count("runtime.stem.evaluate_iteration_proposal("), 1)


if __name__ == "__main__":
    unittest.main()
