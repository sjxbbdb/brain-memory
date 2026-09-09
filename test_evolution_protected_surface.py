"""Regression checks for the immutable promotion/evaluation surface."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

from brain.evaluation_harness import BaselineRevision, EvaluationHarness, directory_fingerprint
from brain.evolution import CandidateIsolationError, PromotionController, PromotionError


class DefaultProtectedSurfaceTests(unittest.TestCase):
    def test_runtime_enforcement_and_discovery_controls_are_protected(self):
        protected = set(PromotionController.DEFAULT_PROTECTED_PATHS)
        expected = {
            ".gitignore",
            "start.bat",
            ".env.example",
            "AGENTS.md",
            "CONTEXT.md",
            "README.md",
            "version.py",
            "config.py",
            "requirements.txt",
            "requirements.lock",
            "requirements-dev.txt",
            "requirements-dev.lock",
            "RELEASE_v0.1.md",
            "RELEASE_v10.0.md",
            "brain/autonomy.py",
            "brain/boundary.py",
            "brain/cognitive_dispatch.py",
            "brain/core.py",
            "brain/evaluation_harness.py",
            "brain/evolution.py",
            "brain/intent.py",
            "brain/learning_feedback.py",
            "brain/life_kernel.py",
            "brain/brain_stem.py",
            "brain/brain_state.py",
            "brain/core_purpose.py",
            "brain/task_execution.py",
            "brain/task_scheduler.py",
            "brain/schemas.py",
            "brain/__init__.py",
            "storage/database.py",
            "storage/__init__.py",
            "agent_bridge.py",
            "agent",
            "api",
            "services/llm_client.py",
            "services/llm_prompts.py",
            "services/source_adapter.py",
            "services/__init__.py",
            "docs/adr",
            "docs/research",
            "docs/verification",
            "conftest.py",
            "pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            "sitecustomize.py",
            "usercustomize.py",
        }
        self.assertEqual(set(), expected - protected)

    def test_every_root_regression_test_is_protected(self):
        repository_root = Path(__file__).resolve().parent
        root_tests = {path.name for path in repository_root.glob("test_*.py")}
        protected = set(PromotionController.DEFAULT_PROTECTED_PATHS)
        self.assertEqual(set(), root_tests - protected)

    def test_candidate_cannot_replace_a_causal_enforcement_module(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-protected-surface-") as temp:
            root = Path(temp)
            active = root / "active"
            candidate = root / "candidate"
            active_module = active / "brain" / "task_execution.py"
            candidate_module = candidate / "brain" / "task_execution.py"
            active_module.parent.mkdir(parents=True)
            candidate_module.parent.mkdir(parents=True)
            active_module.write_text("TRUSTED = True\n", encoding="utf-8")
            candidate_module.write_text("TRUSTED = False\n", encoding="utf-8")

            controller = PromotionController(
                active,
                protected_files=(),
                profile="legacy",
            )

            self.assertIn(
                "brain/task_execution.py",
                controller._candidate_protected_violations(candidate),
            )

    def test_candidate_cannot_replace_package_initializers(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-package-init-") as temp:
            root = Path(temp)
            active = root / "active"
            candidate = root / "candidate"
            for relative in (
                "brain/__init__.py",
                "storage/__init__.py",
                "services/__init__.py",
            ):
                active_path = active / Path(relative)
                candidate_path = candidate / Path(relative)
                active_path.parent.mkdir(parents=True, exist_ok=True)
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                active_path.write_text("TRUSTED = True\n", encoding="utf-8")
                candidate_path.write_text("TRUSTED = False\n", encoding="utf-8")

            controller = PromotionController(active, profile="legacy")
            violations = controller._candidate_protected_violations(candidate)
            self.assertEqual(
                {
                    "brain/__init__.py",
                    "storage/__init__.py",
                    "services/__init__.py",
                },
                set(violations),
            )

    def test_controller_rejects_database_state_inside_runtime_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-database-boundary-") as temp:
            root = Path(temp)
            active = root / "active"
            active.mkdir()
            (active / "program.py").write_text("print('ok')\n", encoding="utf-8")
            (active / "brain_v4.db").write_bytes(b"opaque-state")
            with self.assertRaises(PromotionError):
                PromotionController(active, profile="legacy")

            clean = root / "clean"
            clean.mkdir()
            controller = PromotionController(clean, profile="legacy")
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "program.py").write_text("candidate\n", encoding="utf-8")
            (candidate / "brain.sqlite-wal").write_bytes(b"journal")
            with self.assertRaises(CandidateIsolationError):
                controller._candidate_path(candidate)

    def test_controller_rejects_uncreated_database_path_inside_runtime_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-uncreated-database-") as temp:
            root = Path(temp)
            active = root / "active"
            active.mkdir()
            (active / "program.py").write_text("trusted\n", encoding="utf-8")
            database_path = active / "state" / "brain.sqlite"
            self.assertFalse(database_path.exists())
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    profile="legacy",
                    persistence_path=database_path,
                )

    def test_brain_stem_binds_an_external_persistence_path(self):
        import tempfile

        from brain.brain_stem import BrainStem

        with tempfile.TemporaryDirectory(prefix="promotion-external-binding-") as temp:
            root = Path(temp)
            active = root / "active"
            active.mkdir()
            (active / "program.py").write_text("trusted\n", encoding="utf-8")
            external = root / "persistent" / "brain.sqlite"
            controller = PromotionController(active, profile="legacy")
            stem = BrainStem(
                state_store=SimpleNamespace(db_path=str(external)),
                promotion_controller=controller,
            )
            self.assertEqual(controller.persistence_path, external.resolve())
            self.assertEqual(stem.promotion_controller, controller)

            with self.assertRaises(PromotionError):
                BrainStem(
                    state_store=SimpleNamespace(db_path=str(active / "late.sqlite")),
                    promotion_controller=controller,
                )

    def test_external_database_survives_runtime_tree_swap(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-external-database-") as temp:
            root = Path(temp)
            active = root / "active"
            candidate = root / "candidate"
            fixtures = root / "fixtures"
            active.mkdir()
            candidate.mkdir()
            fixtures.mkdir()
            (active / "program.py").write_text("trusted\n", encoding="utf-8")
            (candidate / "program.py").write_text("better\n", encoding="utf-8")
            (fixtures / "judge.py").write_text(
                "import json\n"
                "print(json.dumps({'verified': True, 'status': 'pass', 'metrics': {'quality': 1.0}}))\n",
                encoding="utf-8",
            )
            external_db = root / "persistent" / "brain.sqlite"
            external_db.parent.mkdir()
            external_db.write_bytes(b"persistent-state")
            before = external_db.read_bytes()
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=[sys.executable, "{fixtures}/judge.py", "{candidate}"],
                primary_dimension="quality",
            )
            baseline = BaselineRevision(
                "baseline",
                directory_fingerprint(active),
                {"quality": 0.5},
                source_path=str(active),
                harness_version=harness.harness_version,
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = harness.evaluate(candidate, baseline)
            self.assertTrue(receipt.accepted, receipt.to_dict())
            controller = PromotionController(active, harness=harness, profile="legacy")
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertTrue(outcome.accepted, outcome)
            self.assertEqual(external_db.read_bytes(), before)

    def test_controller_rejects_a_vcs_or_dependency_checkout_as_runtime_root(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="promotion-runtime-boundary-") as temp:
            root = Path(temp)
            active = root / "active"
            active.mkdir()
            (active / ".git").mkdir()
            (active / ".git" / "config").write_text(
                "[remote \"origin\"]\nurl=https://example.invalid/private.git\n",
                encoding="utf-8",
            )
            with self.assertRaises(PromotionError):
                PromotionController(active, profile="legacy")

            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / ".venv").mkdir()
            (candidate / "program.txt").write_text("candidate", encoding="utf-8")
            clean = root / "clean"
            clean.mkdir()
            controller = PromotionController(clean, profile="legacy")
            with self.assertRaises(CandidateIsolationError):
                controller._candidate_path(candidate)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
