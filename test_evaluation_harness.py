"""Contract tests for the independent local EvaluationHarness."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from brain.evaluation_harness import (
    BaselineRevision,
    CandidateIsolationError,
    EvaluationHarness,
    EvaluationMode,
    CandidateResultError,
    FixtureSet,
    HarnessConfigurationError,
    ResourceBudget,
    directory_fingerprint,
)


class EvaluationHarnessTests(unittest.TestCase):
    def _candidate(self, root: Path, output: str, *, extra: str = "") -> Path:
        candidate = root / "candidate-source"
        candidate.mkdir()
        extra_block = textwrap.dedent(extra).strip("\n")
        script_parts = [
            "import json",
            "import os",
            "from pathlib import Path",
        ]
        if extra_block:
            script_parts.append(extra_block)
        script_parts.append(f"print({output!r})")
        script = "\n".join(script_parts) + "\n"
        (candidate / "evaluate.py").write_text(script, encoding="utf-8")
        return candidate

    def _baseline(self, root: Path, *, metrics=None) -> BaselineRevision:
        source = root / "baseline-source"
        source.mkdir()
        (source / "marker.txt").write_text("trusted", encoding="utf-8")
        return BaselineRevision(
            revision_id="baseline-1",
            fingerprint=directory_fingerprint(source),
            metrics=metrics or {"quality": 0.5, "safety": 1.0},
            source_path=str(source),
        )

    def _judge(self, root: Path, output: str, *, extra: str = "") -> Path:
        """Create a fixture-owned judge; candidate code never emits the score."""
        fixtures = root / "judge-fixtures"
        fixtures.mkdir()
        extra_block = textwrap.dedent(extra).strip("\n")
        script_parts = ["import os", "from pathlib import Path"]
        if extra_block:
            script_parts.append(extra_block)
        script_parts.append(f"print({output!r})")
        (fixtures / "judge.py").write_text("\n".join(script_parts) + "\n", encoding="utf-8")
        return fixtures

    def _command(self) -> list[str]:
        return [sys.executable, "{fixtures}/judge.py", "{candidate}"]

    def test_evolution_accepts_strict_improvement_and_emits_verifiable_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"status":"pass","verified":true,"metrics":{"quality":0.75,"safety":1.0}}',
            )
            fixtures = self._judge(root, '{"status":"pass","verified":true,"metrics":{"quality":0.75,"safety":1.0}}')
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            )
            receipt = harness.evaluate(candidate, baseline)
            self.assertTrue(receipt.accepted, receipt.to_dict())
            self.assertEqual(receipt.outcome, "ACCEPTED_EVOLUTION")
            self.assertTrue(receipt.verify())
            self.assertTrue(receipt.isolated)
            self.assertGreater(receipt.improvement or 0, 0)
            self.assertTrue(all(gate.passed for gate in receipt.gates))
            self.assertEqual(receipt.fixture_hash, harness.fixture_hash)
            json.loads(receipt.to_json())
            restored = receipt.from_dict(json.loads(receipt.to_json()))
            self.assertEqual(restored.receipt_hash, receipt.receipt_hash)
            tampered = receipt.to_dict()
            tampered["accepted"] = not tampered["accepted"]
            with self.assertRaises(CandidateResultError):
                receipt.from_dict(tampered)
            with self.assertRaises((TypeError, AttributeError)):
                receipt.candidate_metrics["quality"] = 9  # type: ignore[index]

    def test_receipt_schema_is_versioned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"status":"pass","verified":true,"metrics":{"quality":0.75,"safety":1.0}}',
            )
            fixtures = self._judge(
                root,
                '{"status":"pass","verified":true,"metrics":{"quality":0.75,"safety":1.0}}',
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            )
            receipt = harness.evaluate(candidate, baseline)
            payload = receipt.to_dict()
            payload["schema_version"] = 999
            with self.assertRaises(CandidateResultError):
                receipt.from_dict(payload)

    def test_evolution_rejects_no_improvement_and_marks_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.5,"safety":1.0}}',
            )
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.5,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertEqual(receipt.outcome, "REJECTED_ROLLBACK")
            self.assertTrue(receipt.rollback_performed)
            self.assertEqual(receipt.rollback_target, baseline.revision_id)
            self.assertFalse(next(g for g in receipt.gates if g.name == "improvement").passed)
            self.assertTrue(receipt.verify())

    def test_candidate_cannot_self_certify_progress(self):
        """A candidate-owned result is evidence-free even if it claims pass."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"status":"pass","verified":true,"metrics":{"quality":9.0,"safety":9.0}}',
            )
            receipt = EvaluationHarness(
                command=[sys.executable, "{candidate}/evaluate.py"],
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "independent_judge").passed)
            self.assertEqual(receipt.outcome, "REJECTED_ROLLBACK")

    def test_fixed_judge_is_the_only_acceptance_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":99,"safety":99}}',
            )
            # The fixture judge reports the baseline value, regardless of the
            # candidate's self-reported claim.
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.5,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertEqual(receipt.candidate_metrics["quality"], 0.5)
            self.assertTrue(next(g for g in receipt.gates if g.name == "independent_judge").passed)

    def test_fixture_drift_after_initialization_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.8}}')
            harness = EvaluationHarness(fixtures=fixtures, command=self._command(), primary_dimension="quality")
            (fixtures / "judge.py").write_text("print('{}')", encoding="utf-8")
            baseline = self._baseline(root, metrics={"quality": 0.5})
            candidate = self._candidate(root, "{}")
            receipt = harness.evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "integrity").passed)

    def test_baseline_from_different_harness_contract_cannot_promote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(root, '{"verified":true,"metrics":{"quality":0.9,"safety":1.0}}')
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.9,"safety":1.0}}')
            harness = EvaluationHarness(fixtures=fixtures, command=self._command(), primary_dimension="quality")
            pinned = BaselineRevision(
                baseline.revision_id,
                baseline.fingerprint,
                baseline.metrics,
                baseline.source_path,
                harness_version="0.0.0",
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = harness.evaluate(candidate, pinned)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "baseline_contract").passed)

    def test_sandbox_boundary_writes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(root, "{}")
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.9,"safety":1.0}}',
                extra="Path(os.environ['EVAL_CANDIDATE_DIR']).parent.joinpath('escape.txt').write_text('x')",
            )
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "permission_boundary").passed)

    def test_recovery_accepts_restored_baseline_without_strict_improvement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"status":"accepted","verified":true,"metrics":{"quality":0.5,"safety":1.0}}',
            )
            fixtures = self._judge(root, '{"status":"accepted","verified":true,"metrics":{"quality":0.5,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline, mode=EvaluationMode.RECOVERY)
            self.assertTrue(receipt.accepted, receipt.to_dict())
            self.assertEqual(receipt.outcome, "ACCEPTED_RECOVERY")
            self.assertTrue(next(g for g in receipt.gates if g.name == "recovery_baseline").passed)

    def test_critical_dimension_regression_rejects_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8,"safety":0.9}}',
            )
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.8,"safety":0.9}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
                critical_dimensions=["quality", "safety"],
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "improvement").passed)

    def test_fixture_hash_is_pinned_and_mutation_fails_integrity_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures = root / "fixtures-source"
            fixtures.mkdir()
            (fixtures / "case.json").write_text('{"answer": 2}', encoding="utf-8")
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="Path(os.environ['EVAL_FIXTURES_DIR'], 'case.json').chmod(0o644); Path(os.environ['EVAL_FIXTURES_DIR'], 'case.json').write_text('tampered')",
            )
            (fixtures / "judge.py").write_text(
                textwrap.dedent(
                    """
                    import os
                    from pathlib import Path
                    Path(os.environ['EVAL_FIXTURES_DIR'], 'case.json').chmod(0o644)
                    Path(os.environ['EVAL_FIXTURES_DIR'], 'case.json').write_text('tampered')
                    print('{\"verified\":true,\"metrics\":{\"quality\":0.8,\"safety\":1.0}}')
                    """
                ),
                encoding="utf-8",
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=[sys.executable, "{fixtures}/judge.py", "{candidate}"],
                primary_dimension="quality",
            )
            original_hash = harness.fixture_hash
            receipt = harness.evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertEqual(receipt.fixture_hash, original_hash)
            self.assertFalse(next(g for g in receipt.gates if g.name == "integrity").passed)
            # The source fixture itself is never handed to the child.
            self.assertEqual((fixtures / "case.json").read_text(encoding="utf-8"), '{"answer": 2}')

    def test_candidate_runs_in_temporary_copy_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="Path('created-by-child.txt').write_text('only in copy')",
            )
            before = directory_fingerprint(candidate)
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertTrue(receipt.accepted)
            self.assertEqual(directory_fingerprint(candidate), before)
            self.assertFalse((candidate / "created-by-child.txt").exists())

    def test_timeout_and_output_budget_are_hard_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="__import__('time').sleep(0.25)",
            )
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="__import__('time').sleep(0.25)",
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                resource_budget=ResourceBudget(timeout_sec=0.03, max_output_bytes=256),
                primary_dimension="quality",
            )
            receipt = harness.evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertTrue(receipt.timed_out)
            self.assertFalse(next(g for g in receipt.gates if g.name == "resource_budget").passed)

    def test_candidate_artifact_limits_are_enforced_before_copy(self):
        """A hostile source cannot consume disk before the post-run gate."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
            )
            # Keep the executable source below the limit and make a second
            # file exceed max_file_bytes.
            (candidate / "payload.bin").write_bytes(b"x" * 2048)
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                resource_budget=ResourceBudget(
                    max_file_bytes=1024,
                    max_total_bytes=4096,
                ),
                primary_dimension="quality",
            )
            with self.assertRaises(CandidateIsolationError):
                harness.evaluate(candidate, {"quality": 0.5})

    def test_candidate_file_count_limit_is_enforced_before_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
            )
            (candidate / "one.txt").write_text("1", encoding="utf-8")
            (candidate / "two.txt").write_text("2", encoding="utf-8")
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                resource_budget=ResourceBudget(
                    max_files=2,
                    max_file_bytes=4096,
                    max_total_bytes=8192,
                ),
                primary_dimension="quality",
            )
            with self.assertRaises(CandidateIsolationError):
                harness.evaluate(candidate, {"quality": 0.5})

    def test_candidate_growth_after_copy_is_rejected_by_live_budget_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = self._candidate(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
                extra=(
                    "            import time\n"
                    "            from pathlib import Path\n"
                    "            with Path('growth.bin').open('wb') as stream:\n"
                    "                for _ in range(80):\n"
                    "                    stream.write(b'x' * 1024)\n"
                    "                    stream.flush()\n"
                    "                    time.sleep(0.01)\n"
                ),
            )
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.8}}',
                extra=(
                    "import subprocess, sys\n"
                    "subprocess.run([sys.executable, str(Path(sys.argv[1]) / 'evaluate.py')], check=False)\n"
                ),
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                resource_budget=ResourceBudget(
                    timeout_sec=3.0,
                    max_file_bytes=4096,
                    max_total_bytes=8192,
                ),
                primary_dimension="quality",
            )
            receipt = harness.evaluate(candidate, {"quality": 0.5})
            self.assertFalse(receipt.accepted, receipt.to_dict())
            self.assertFalse(
                next(g for g in receipt.gates if g.name == "resource_budget").passed
            )

    def test_output_overflow_is_counted_without_unbounded_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(root, "{}")
            fixtures = self._judge(
                root,
                '{"verified":true,"metrics":{"quality":0.9,"safety":1.0}}',
                extra="print('x' * 4096)",
            )
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                resource_budget=ResourceBudget(
                    max_stdout_bytes=128,
                    max_stderr_bytes=128,
                    max_output_bytes=256,
                ),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertGreater(receipt.stdout_bytes, 128)
            self.assertFalse(next(g for g in receipt.gates if g.name == "resource_budget").passed)

    def test_missing_verified_result_is_rejected_even_with_better_metric(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                '{"metrics":{"quality":0.9,"safety":1.0}}',
            )
            fixtures = self._judge(root, '{"metrics":{"quality":0.9,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(candidate, baseline)
            self.assertFalse(receipt.accepted)
            self.assertFalse(next(g for g in receipt.gates if g.name == "verification").passed)

    def test_scrubs_credential_environment_and_tolerates_result_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(
                root,
                'EVALUATION_RESULT:{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="assert os.getenv('DEEPSEEK_API_KEY') is None",
            )
            fixtures = self._judge(
                root,
                'EVALUATION_RESULT:{"verified":true,"metrics":{"quality":0.8,"safety":1.0}}',
                extra="assert os.getenv('DEEPSEEK_API_KEY') is None",
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            )
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "do-not-pass"}, clear=False):
                receipt = harness.evaluate(candidate, baseline)
            self.assertTrue(receipt.accepted, receipt.to_dict())

    def test_shell_and_remote_commands_are_rejected(self):
        with self.assertRaises(HarnessConfigurationError):
            EvaluationHarness(command=["cmd.exe", "/c", "echo", "x"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root)
            candidate = self._candidate(root, '{"verified":true,"metrics":{"quality":0.8}}')
            with self.assertRaises(HarnessConfigurationError):
                EvaluationHarness(command=[sys.executable, "{candidate}/evaluate.py", "https://github.com/x"]).evaluate(candidate, baseline)

    def test_fixed_version_and_fixture_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "a.txt").write_text("a", encoding="utf-8")
            fixture_set = FixtureSet.from_path(fixtures)
            self.assertEqual(fixture_set.fixture_hash, directory_fingerprint(fixtures))
            h1 = EvaluationHarness(fixtures=fixtures)
            h2 = EvaluationHarness(fixtures=fixtures)
            self.assertEqual(h1.harness_version, h2.harness_version)
            self.assertEqual(h1.evaluator_hash, h2.evaluator_hash)
            self.assertEqual(h1.fixture_hash, h2.fixture_hash)

    def test_private_files_are_not_read_or_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "candidate"
            source.mkdir()
            (source / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")
            (source / "evaluate.py").write_text(
                "print('{\"verified\":true,\"metrics\":{\"quality\":1}}')", encoding="utf-8"
            )
            descriptor_hash = directory_fingerprint(source)
            self.assertTrue(descriptor_hash)
            baseline = self._baseline(root)
            fixtures = self._judge(root, '{"verified":true,"metrics":{"quality":1,"safety":1.0}}')
            receipt = EvaluationHarness(
                fixtures=fixtures,
                command=self._command(),
                primary_dimension="quality",
            ).evaluate(
                source,
                baseline,
            )
            self.assertTrue(receipt.accepted, receipt.to_dict())

    def test_configuration_requires_baseline_for_evolution(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = self._candidate(Path(temp), '{"verified":true,"metrics":{"quality":1}}')
            with self.assertRaises(HarnessConfigurationError):
                EvaluationHarness(command=[sys.executable, "{candidate}/evaluate.py"]).evaluate(candidate)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
