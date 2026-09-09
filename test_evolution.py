"""Behaviour tests for the local evolution/promotion seam.

These tests deliberately use a fixture-owned judge.  A candidate is only a
source tree and cannot manufacture the acceptance receipt itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from brain.evaluation_harness import (
    BaselineRevision,
    EvaluationHarness,
    EvaluationMode,
    EvaluationReceipt,
    directory_fingerprint,
)
from brain import evolution as evolution_module
from brain.evolution import (
    AuthorizationError,
    CandidateIsolationError,
    CandidateSandbox,
    EVOLUTION,
    LedgerError,
    RECOVERY,
    PromotionController,
    PromotionError,
    PromotionLedger,
    PromotionManifest,
    ProtectedFileError,
    SandboxAttestationError,
    SandboxAttestor,
)


class EvolutionHarnessTests(unittest.TestCase):
    def _fixture_harness(self, root: Path, score: float = 2.0) -> EvaluationHarness:
        fixtures = root / "fixtures"
        fixtures.mkdir(exist_ok=True)
        # The judge is copied by EvaluationHarness and is the sole authority
        # for the score.  It intentionally does not import or execute code
        # from the candidate tree.
        payload = json.dumps(
            {"verified": True, "status": "pass", "metrics": {"quality": score}}
        )
        (fixtures / "judge.py").write_text(
            f"print({payload!r})\n", encoding="utf-8"
        )
        return EvaluationHarness(
            fixtures=fixtures,
            command=[sys.executable, "{fixtures}/judge.py", "{candidate}"],
            primary_dimension="quality",
        )

    def _tree(self, root: Path, name: str, *, quality: str = "candidate") -> Path:
        path = root / name
        path.mkdir()
        (path / "program.txt").write_text(quality, encoding="utf-8")
        (path / "protected.txt").write_text("trusted-anchor", encoding="utf-8")
        return path

    def _receipt(
        self,
        root: Path,
        active: Path,
        candidate: Path,
        *,
        mode: EvaluationMode | str = EVOLUTION,
        score: float = 2.0,
    ) -> tuple[EvaluationHarness, EvaluationReceipt]:
        harness = self._fixture_harness(root, score)
        baseline = BaselineRevision.from_path(
            active,
            metrics={"quality": 1.0},
            revision_id="active-1",
            harness_version=harness.harness_version,
            fixture_hash=harness.fixture_hash,
            evaluator_hash=harness.evaluator_hash,
        )
        receipt = harness.evaluate(candidate, baseline, mode=mode)
        self.assertTrue(receipt.verify(), receipt.to_dict())
        return harness, receipt

    def test_candidate_sandbox_is_a_disposable_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._tree(root, "candidate")
            before = directory_fingerprint(source)
            with CandidateSandbox(source) as sandbox:
                self.assertNotEqual(sandbox.path.resolve(), source.resolve())
                self.assertEqual(sandbox.fingerprint, before)
                (sandbox.path / "program.txt").write_text("sandbox-only", encoding="utf-8")
                self.assertTrue(sandbox.verify())
            self.assertEqual(directory_fingerprint(source), before)
            self.assertEqual((source / "program.txt").read_text(encoding="utf-8"), "candidate")

    def test_rejected_candidate_never_touches_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="same")
            harness, receipt = self._receipt(root, active, candidate, score=1.0)
            before = directory_fingerprint(active)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertFalse(outcome.accepted)
            self.assertEqual(outcome.action, "rejected")
            self.assertEqual(directory_fingerprint(active), before)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertEqual(controller.ledger.events[-1].action, "rejected")

    def test_authorization_is_required_before_any_active_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            before = directory_fingerprint(active)
            controller = PromotionController(active, harness=harness, profile="legacy")
            with self.assertRaises(AuthorizationError):
                controller.promote(candidate, receipt)
            self.assertEqual(directory_fingerprint(active), before)
            self.assertFalse(controller.rollback_available)

    def test_protected_files_are_immutable_during_promotion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            (candidate / "protected.txt").write_text("tampered", encoding="utf-8")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertFalse(outcome.accepted)
            self.assertIn("protected", outcome.reason.lower())
            self.assertEqual((active / "protected.txt").read_text(encoding="utf-8"), "trusted-anchor")
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "candidate")

    def test_successful_evolution_preserves_protected_file_and_can_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            old_fingerprint = directory_fingerprint(active)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )
            outcome = controller.promote(candidate, receipt, authorized=True, mode=EVOLUTION)
            self.assertTrue(outcome.accepted, outcome)
            self.assertEqual(outcome.action, "promoted")
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            self.assertEqual((active / "protected.txt").read_text(encoding="utf-8"), "trusted-anchor")
            self.assertTrue(controller.rollback_available)
            self.assertTrue(controller.ledger.verify())
            rollback = controller.rollback(authorized=True)
            self.assertTrue(rollback.accepted, rollback)
            self.assertEqual(rollback.action, "rolled_back")
            self.assertEqual(directory_fingerprint(active), old_fingerprint)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertTrue(controller.ledger.verify())

    def test_promotion_manifest_survives_close_and_reopen(self):
        """A controller close must not erase the only durable rollback point."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-reopen-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            promoted = controller.promote(candidate, receipt, authorized=True)
            self.assertTrue(promoted.accepted, promoted)
            manifest_path = controller.manifest_path
            self.assertTrue(manifest_path.is_file())
            controller.close()
            self.assertTrue(controller.rollback_available)

            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertTrue(reopened.rollback_available)
            rolled_back = reopened.rollback(authorized=True)
            self.assertTrue(rolled_back.accepted, rolled_back)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertTrue(reopened.verify())

    def test_manifest_recovers_after_first_swap_rename_crash(self):
        """A crash after active→backup restores the trusted tree on reopen."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-first-crash-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )

            def crash_after_first_rename(stage):
                backup = controller._planned_backup_path
                self.assertIsNotNone(backup)
                os.replace(active, backup)
                raise KeyboardInterrupt("simulated process crash")

            controller._swap_in = crash_after_first_rename
            with self.assertRaises(KeyboardInterrupt):
                controller.promote(candidate, receipt, authorized=True)
            self.assertFalse(active.exists())
            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertTrue(active.is_dir())
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertFalse(reopened.rollback_available)
            self.assertEqual(len(reopened.ledger.events), 0)
            self.assertTrue(reopened.verify())

    def test_manifest_recovers_after_second_swap_rename_before_receipt(self):
        """A crash after stage→active but before the ledger append rolls back."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-second-crash-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )

            def crash_after_second_rename(stage):
                backup = controller._planned_backup_path
                self.assertIsNotNone(backup)
                os.replace(active, backup)
                os.replace(stage, active)
                raise KeyboardInterrupt("simulated process crash")

            controller._swap_in = crash_after_second_rename
            with self.assertRaises(KeyboardInterrupt):
                controller.promote(candidate, receipt, authorized=True)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertEqual(len(reopened.ledger.events), 0)
            self.assertTrue(reopened.verify())

    def test_manifest_adopts_durable_receipt_after_commit_write_crash(self):
        """A durable promoted event is enough to finish COMMITTED on reopen."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-commit-crash-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            original_persist = controller._persist_manifest

            def crash_before_committed(manifest):
                if manifest.phase == "COMMITTED":
                    raise KeyboardInterrupt("simulated process crash")
                return original_persist(manifest)

            controller._persist_manifest = crash_before_committed
            with self.assertRaises(KeyboardInterrupt):
                controller.promote(candidate, receipt, authorized=True)
            self.assertEqual(len(PromotionLedger.from_path(ledger_path).events), 1)
            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertTrue(reopened.rollback_available)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            reopened.rollback(authorized=True)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")

    def test_manifest_recovers_after_rollback_first_rename_crash(self):
        with tempfile.TemporaryDirectory(prefix="promotion-manifest-rollback-first-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            original_replace = evolution_module.os.replace
            crashed = {"value": False}

            def crash_after_current_rename(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                result = original_replace(source, destination)
                if (
                    not crashed["value"]
                    and source_path == active
                    and destination_path.name.startswith(".brain-memory-rollback-current-")
                ):
                    crashed["value"] = True
                    raise KeyboardInterrupt("simulated rollback crash")
                return result

            with mock.patch.object(evolution_module.os, "replace", crash_after_current_rename):
                with self.assertRaises(KeyboardInterrupt):
                    controller.rollback(authorized=True)
            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            self.assertTrue(reopened.rollback_available)
            self.assertTrue(reopened.verify())

    def test_manifest_recovers_after_rollback_second_rename_before_receipt(self):
        with tempfile.TemporaryDirectory(prefix="promotion-manifest-rollback-second-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            manifest = json.loads(controller.manifest_path.read_text(encoding="utf-8"))
            backup = root / manifest["backup_name"]
            original_replace = evolution_module.os.replace
            crashed = {"value": False}

            def crash_after_backup_rename(source, destination):
                result = original_replace(source, destination)
                if (
                    not crashed["value"]
                    and Path(source) == backup
                    and Path(destination) == active
                ):
                    crashed["value"] = True
                    raise KeyboardInterrupt("simulated rollback crash")
                return result

            with mock.patch.object(evolution_module.os, "replace", crash_after_backup_rename):
                with self.assertRaises(KeyboardInterrupt):
                    controller.rollback(authorized=True)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            reopened = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            self.assertTrue(reopened.rollback_available)
            self.assertTrue(reopened.verify())

    def test_tampered_manifest_backup_is_rejected_without_replacing_active(self):
        """Recovery validates backup bytes before any rename."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-tamper-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=ledger_path,
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            manifest = json.loads(controller.manifest_path.read_text(encoding="utf-8"))
            backup = root / manifest["backup_name"]
            (backup / "program.txt").write_text("tampered", encoding="utf-8")
            active_before = directory_fingerprint(active)
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    harness=harness,
                    protected_files=("protected.txt",),
                    ledger_path=ledger_path,
                    profile="legacy",
                )
            self.assertEqual(directory_fingerprint(active), active_before)
            self.assertTrue(backup.is_dir())

    def test_rollback_rejects_active_or_backup_drift(self):
        with tempfile.TemporaryDirectory(prefix="promotion-manifest-rollback-drift-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=root / "promotion.jsonl",
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            (active / "program.txt").write_text("external", encoding="utf-8")
            with self.assertRaises(PromotionError):
                controller.rollback(authorized=True)

            # Restore the active candidate and tamper only the rollback source.
            (active / "program.txt").write_text("better", encoding="utf-8")
            manifest = json.loads(controller.manifest_path.read_text(encoding="utf-8"))
            backup = root / manifest["backup_name"]
            (backup / "program.txt").write_text("external-old", encoding="utf-8")
            with self.assertRaises(PromotionError):
                controller.rollback(authorized=True)

    def test_explicit_discard_is_authorized_and_close_is_non_destructive(self):
        with tempfile.TemporaryDirectory(prefix="promotion-manifest-discard-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion.jsonl"
            controller = PromotionController(
                active,
                harness=harness,
                ledger_path=ledger_path,
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            controller.close()
            self.assertTrue(controller.rollback_available)
            with self.assertRaises(Exception):
                controller.discard_rollback()
            outcome = controller.discard_rollback(authorized=True)
            self.assertTrue(outcome.accepted, outcome)
            self.assertFalse(controller.rollback_available)
            reopened = PromotionController(
                active,
                harness=harness,
                ledger_path=ledger_path,
                profile="legacy",
            )
            self.assertFalse(reopened.rollback_available)
            self.assertTrue(reopened.verify())

    def test_corrupt_manifest_makes_verify_return_false(self):
        with tempfile.TemporaryDirectory(prefix="promotion-manifest-corrupt-") as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            controller.manifest_path.write_text("{bad", encoding="utf-8")
            self.assertFalse(controller.verify())

    def test_verify_does_not_reload_or_reconcile_the_file_backed_ledger(self):
        """The health check observes state; it never repairs the ledger cache."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-verify-readonly-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=root / "promotion.jsonl",
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)

            def unexpected_reload():
                raise AssertionError("verify must not reload or reconcile state")

            controller.ledger.reload = unexpected_reload  # type: ignore[method-assign]
            self.assertTrue(controller.verify())

    def test_rolled_back_manifest_rejects_active_drift_before_cleanup(self):
        """A crash after the rollback marker cannot bless a tampered active tree."""

        with tempfile.TemporaryDirectory(prefix="promotion-manifest-rolled-back-drift-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                ledger_path=root / "promotion.jsonl",
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)
            original_persist = controller._persist_manifest

            def crash_before_idle(manifest):
                if manifest.phase == "IDLE":
                    raise KeyboardInterrupt("simulated cleanup crash")
                return original_persist(manifest)

            controller._persist_manifest = crash_before_idle
            with self.assertRaises(KeyboardInterrupt):
                controller.rollback(authorized=True)
            (active / "program.txt").write_text("tampered-after-rollback", encoding="utf-8")
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    harness=harness,
                    protected_files=("protected.txt",),
                    ledger_path=root / "promotion.jsonl",
                    profile="legacy",
                )

    def test_constitutional_protected_floor_cannot_be_disabled_by_empty_custom_list(self):
        """Project safety anchors remain protected even when a host adds none."""

        with tempfile.TemporaryDirectory(prefix="promotion-mandatory-protected-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            (active / "version.py").write_text("PRODUCT_VERSION = '0.1.0'\n", encoding="utf-8")
            (candidate / "version.py").write_text("PRODUCT_VERSION = '9.9.9'\n", encoding="utf-8")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=(),
                profile="legacy",
            )
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertFalse(outcome.accepted)
            self.assertIn("version.py", outcome.reason)

    def test_manifest_rejects_duplicate_sibling_names(self):
        root_digest = "a" * 24
        name = ".brain-memory-backup-" + "b" * 32
        with self.assertRaises(PromotionError):
            PromotionManifest(
                root_digest=root_digest,
                phase="PREPARED",
                operation_id="op-1",
                backup_name=name,
                current_name=name,
                stage_name=".brain-memory-stage-test",
                before_fingerprint="c" * 64,
                candidate_fingerprint="d" * 64,
                receipt_hash="e" * 64,
                mode=EVOLUTION,
            )

    def test_manifest_accepts_pre_stage_fingerprint_schema_one_hash(self):
        """Existing schema-one crash records remain readable after hardening."""

        manifest = PromotionManifest(
            root_digest="a" * 24,
            phase="PREPARED",
            operation_id="legacy-op",
            backup_name=".brain-memory-backup-" + "b" * 32,
            stage_name=".brain-memory-stage-legacy",
            before_fingerprint="c" * 64,
            candidate_fingerprint="d" * 64,
            receipt_hash="e" * 64,
            mode=EVOLUTION,
        )
        legacy_payload = manifest.to_dict()
        legacy_payload.pop("stage_fingerprint")
        legacy_payload["manifest_hash"] = evolution_module._manifest_payload_hash(
            {
                key: value
                for key, value in legacy_payload.items()
                if key != "manifest_hash"
            }
        )

        restored = PromotionManifest.from_dict(legacy_payload)

        self.assertIsNone(restored.stage_fingerprint)
        self.assertTrue(restored.verify())

    def test_manifest_rejects_tampered_legacy_hash(self):
        manifest = PromotionManifest(
            root_digest="a" * 24,
            phase="PREPARED",
            operation_id="legacy-op",
            backup_name=".brain-memory-backup-" + "b" * 32,
            stage_name=".brain-memory-stage-legacy",
            before_fingerprint="c" * 64,
            candidate_fingerprint="d" * 64,
            receipt_hash="e" * 64,
            mode=EVOLUTION,
        )
        legacy_payload = manifest.to_dict()
        legacy_payload.pop("stage_fingerprint")
        legacy_payload["manifest_hash"] = "0" * 64

        with self.assertRaises(PromotionError):
            PromotionManifest.from_dict(legacy_payload)

    def test_recovery_refuses_a_replaced_staging_tree(self):
        """A same-named foreign stage must remain for operator inspection."""

        with tempfile.TemporaryDirectory(prefix="promotion-stage-owner-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )

            def crash_before_swap(stage):
                raise KeyboardInterrupt("simulated process crash")

            controller._swap_in = crash_before_swap
            with self.assertRaises(KeyboardInterrupt):
                controller.promote(candidate, receipt, authorized=True)
            manifest = json.loads(controller.manifest_path.read_text(encoding="utf-8"))
            stage = root / manifest["stage_name"]
            (stage / "program.txt").write_text("foreign", encoding="utf-8")
            with self.assertRaises(PromotionError):
                PromotionController(active, harness=harness, profile="legacy")
            self.assertTrue(stage.is_dir())
            self.assertEqual((stage / "program.txt").read_text(encoding="utf-8"), "foreign")
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")

    def test_recovery_refuses_a_replaced_rollback_current_tree(self):
        """Rollback cleanup never recursively deletes a foreign replacement."""

        with tempfile.TemporaryDirectory(prefix="promotion-current-owner-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )
            controller.promote(candidate, receipt, authorized=True)

            def crash_before_current_cleanup(path, **kwargs):
                raise KeyboardInterrupt("simulated cleanup crash")

            controller._remove_manifest_owned = crash_before_current_cleanup
            with self.assertRaises(KeyboardInterrupt):
                controller.rollback(authorized=True)
            manifest = json.loads(controller.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "ROLLED_BACK")
            current = root / manifest["current_name"]
            (current / "program.txt").write_text("foreign", encoding="utf-8")
            with self.assertRaises(PromotionError):
                PromotionController(active, harness=harness, profile="legacy")
            self.assertTrue(current.is_dir())
            self.assertEqual((current / "program.txt").read_text(encoding="utf-8"), "foreign")

    def test_recovery_does_not_adopt_an_unauthorized_promoted_event(self):
        """Ledger integrity without explicit authorization is not swap proof."""

        with tempfile.TemporaryDirectory(prefix="promotion-event-auth-guard-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=("protected.txt",),
                profile="legacy",
            )
            before = directory_fingerprint(active)
            candidate_fingerprint = directory_fingerprint(candidate)
            stage = controller._stage_candidate(candidate)
            stage_fingerprint = directory_fingerprint(stage)
            backup = controller._new_sibling("backup")
            os.replace(active, backup)
            os.replace(stage, active)
            operation_id = "forged-operation"
            manifest = PromotionManifest(
                root_digest=controller._root_digest,
                phase="SWAPPED",
                operation_id=operation_id,
                backup_name=backup.name,
                before_fingerprint=before,
                candidate_fingerprint=candidate_fingerprint,
                after_fingerprint=stage_fingerprint,
                receipt_hash=receipt.receipt_hash,
                mode=EVOLUTION,
            )
            controller._persist_manifest(manifest)
            controller.ledger.append(
                action="promoted",
                mode=EVOLUTION,
                candidate_revision_id=receipt.candidate_revision_id,
                candidate_fingerprint=candidate_fingerprint,
                evaluation_receipt_hash=receipt.receipt_hash,
                active_before_fingerprint=before,
                active_after_fingerprint=stage_fingerprint,
                authorized=False,
                metadata={"promotion_operation_id": operation_id},
            )
            reopened = PromotionController(active, harness=harness, profile="legacy")
            self.assertFalse(reopened.rollback_available)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertTrue(reopened.verify())

    def test_sensitive_nested_active_path_blocks_whole_tree_promotion(self):
        with tempfile.TemporaryDirectory(prefix="promotion-sensitive-active-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            private_dir = active / "runtime"
            private_dir.mkdir()
            (private_dir / ".env").write_text("TOKEN=do-not-read", encoding="utf-8")
            with self.assertRaises(ProtectedFileError):
                PromotionController(active, profile="legacy")

    def test_sensitive_candidate_path_blocks_before_evaluation(self):
        with tempfile.TemporaryDirectory(prefix="promotion-sensitive-candidate-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            (candidate / "secret.txt").write_text("do-not-read", encoding="utf-8")
            with self.assertRaises(CandidateIsolationError):
                PromotionController(active, profile="legacy")._candidate_path(candidate)

    def test_orphaned_controller_sibling_blocks_new_controller(self):
        with tempfile.TemporaryDirectory(prefix="promotion-orphan-guard-") as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            orphan = root / (".brain-memory-backup-" + "0" * 32)
            orphan.mkdir()
            (orphan / "program.txt").write_text("orphan", encoding="utf-8")
            with self.assertRaises(PromotionError):
                PromotionController(active, profile="legacy")

    def test_ledger_cannot_collide_with_controller_manifest_or_lock(self):
        with tempfile.TemporaryDirectory(prefix="promotion-ledger-collision-") as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            controller = PromotionController(active, profile="legacy")
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    profile="legacy",
                    ledger_path=controller.manifest_path,
                )
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    profile="legacy",
                    ledger_path=controller._controller_lock_path,
                )

    def test_second_promotion_is_rejected_until_the_previous_backup_is_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            controller = PromotionController(active, harness=harness, profile="legacy")
            first = controller.promote(candidate, receipt, authorized=True)
            self.assertTrue(first.accepted, first)
            before = directory_fingerprint(active)
            with self.assertRaises(PromotionError):
                controller.promote(candidate, receipt, authorized=True)
            self.assertEqual(directory_fingerprint(active), before)
            self.assertTrue(controller.rollback_available)
            self.assertEqual([event.action for event in controller.ledger.events], ["promoted"])

    def test_ledger_failure_after_swap_restores_active_without_a_promotion_receipt(self):
        class FailingLedger(PromotionLedger):
            def append(self, **kwargs):  # type: ignore[override]
                if kwargs.get("action") == "promoted":
                    raise LedgerError("injected append failure")
                return super().append(**kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            before = directory_fingerprint(active)
            ledger = FailingLedger()
            controller = PromotionController(
                active, harness=harness, ledger=ledger, profile="legacy"
            )
            with self.assertRaises(PromotionError):
                controller.promote(candidate, receipt, authorized=True)
            self.assertEqual(directory_fingerprint(active), before)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            self.assertFalse(controller.rollback_available)
            self.assertEqual(len(controller.ledger.events), 0)
            self.assertTrue(controller.verify())

    def test_production_profile_requires_a_host_attestation(self):
        """Receipt.isolated cannot stand in for an OS-enforced host proof."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            before = directory_fingerprint(active)
            controller = PromotionController(
                active,
                harness=harness,
                ledger_path=root / "promotion.jsonl",
                persistence_path=root / "brain.sqlite",
            )
            self.assertEqual(controller.profile, "production")
            with self.assertRaises(SandboxAttestationError):
                controller.promote(candidate, receipt, authorized=True)
            self.assertEqual(directory_fingerprint(active), before)
            self.assertFalse(controller.rollback_available)
            self.assertEqual(controller.ledger.events[-1].action, "attestation_denied")

    def test_host_attestation_binds_receipt_and_is_one_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            attestor = SandboxAttestor(secret=b"host-attestor-secret-0123456789")
            attestation = attestor.issue_for_receipt(receipt)
            controller = PromotionController(
                active,
                harness=harness,
                sandbox_attestor=attestor,
                ledger_path=root / "promotion.jsonl",
                persistence_path=root / "brain.sqlite",
            )
            outcome = controller.promote(
                candidate,
                receipt,
                authorized=True,
                sandbox_attestation=attestation,
            )
            self.assertTrue(outcome.accepted, outcome)
            self.assertTrue(outcome.attestation_verified)
            self.assertEqual(outcome.sandbox_attestation_id, attestation.attestation_id)
            self.assertEqual(
                controller.ledger.events[-1].metadata["sandbox_attestation_id"],
                attestation.attestation_id,
            )
            controller.rollback(authorized=True)
            # Replaying the same host proof after rollback is not accepted.
            with self.assertRaises(SandboxAttestationError):
                controller.promote(
                    candidate,
                    receipt,
                    authorized=True,
                    sandbox_attestation=attestation,
                )
            self.assertEqual(
                [event.action for event in controller.ledger.events],
                ["promoted", "rolled_back", "attestation_denied"],
            )

    def test_forged_attestation_payload_or_capabilities_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            attestor = SandboxAttestor(secret=b"host-attestor-secret-0123456789")
            attestation = attestor.issue_for_receipt(receipt)
            forged = attestation.to_dict()
            forged["signature"] = "0" * 64
            controller = PromotionController(
                active,
                harness=harness,
                sandbox_attestor=attestor,
                ledger_path=root / "promotion.jsonl",
                persistence_path=root / "brain.sqlite",
            )
            with self.assertRaises(SandboxAttestationError):
                controller.promote(
                    candidate,
                    receipt,
                    authorized=True,
                    sandbox_attestation=forged,
                )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "candidate")

    def test_legacy_profile_is_explicit_and_cannot_disable_production(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            harness = self._fixture_harness(root)
            legacy = PromotionController(active, harness=harness, profile="legacy")
            self.assertFalse(legacy.require_sandbox_attestation)
            with self.assertRaises(PromotionError):
                PromotionController(
                    active,
                    harness=harness,
                    profile="production",
                    require_sandbox_attestation=False,
                    ledger_path=root / "production-ledger.jsonl",
                    persistence_path=root / "production.sqlite",
                )

    def test_production_profile_requires_external_persistence_and_file_ledger(self):
        with tempfile.TemporaryDirectory(prefix="promotion-production-config-") as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            external_db = root / "brain.sqlite"
            ledger_path = root / "promotion.jsonl"
            with self.assertRaisesRegex(PromotionError, "persistence_path"):
                PromotionController(active, ledger_path=ledger_path)
            with self.assertRaisesRegex(PromotionError, "file-backed"):
                PromotionController(active, persistence_path=external_db)
            with self.assertRaisesRegex(PromotionError, "file-backed"):
                PromotionController(
                    active,
                    ledger=PromotionLedger(),
                    persistence_path=external_db,
                )
            controller = PromotionController(
                active,
                ledger_path=ledger_path,
                persistence_path=external_db,
            )
            self.assertEqual(controller.profile, "production")
            self.assertEqual(controller.ledger.path, ledger_path.resolve())
            self.assertEqual(controller.persistence_path, external_db.resolve())

    def test_recovery_accepts_baseline_metrics_but_evolution_receipt_cannot_cross_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="repair")
            harness, recovery = self._receipt(
                root, active, candidate, mode=RECOVERY, score=1.0
            )
            controller = PromotionController(active, harness=harness, profile="legacy")
            accepted = controller.promote(
                candidate, recovery, authorized=True, mode=RECOVERY
            )
            self.assertTrue(accepted.accepted, accepted)

            # The same fixed receipt cannot be reclassified as Evolution.
            with self.assertRaises(PromotionError):
                controller.promote(candidate, recovery, authorized=True, mode=EVOLUTION)

    def test_tampered_or_candidate_owned_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            payload = receipt.to_dict()
            payload["accepted"] = False
            with self.assertRaises(PromotionError):
                PromotionController(active, harness=harness, profile="legacy").promote(
                    candidate, payload, authorized=True
                )

            class CandidateClaim:
                accepted = True
                mode = EVOLUTION

            with self.assertRaises(PromotionError):
                PromotionController(active, harness=harness, profile="legacy").promote(
                    candidate, CandidateClaim(), authorized=True
                )

    def test_candidate_mutation_after_evaluation_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            (candidate / "program.txt").write_text("changed-after-judge", encoding="utf-8")
            controller = PromotionController(active, harness=harness, profile="legacy")
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertFalse(outcome.accepted)
            self.assertIn("fingerprint", outcome.reason.lower())
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "candidate")

    def test_ledger_is_append_only_hash_chained_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            candidate = self._tree(root, "candidate", quality="better")
            harness, receipt = self._receipt(root, active, candidate)
            ledger_path = root / "promotion-ledger.jsonl"
            controller = PromotionController(
                active, harness=harness, ledger_path=ledger_path, profile="legacy"
            )
            outcome = controller.promote(candidate, receipt, authorized=True)
            self.assertTrue(outcome.accepted)
            lines_before = ledger_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines_before), 1)
            self.assertTrue(controller.ledger.verify())

            restored = controller.ledger.from_path(ledger_path)
            self.assertTrue(restored.verify())
            self.assertEqual(restored.events[0].event_hash, controller.ledger.events[0].event_hash)
            # Any edit to an existing line invalidates the chain; callers must
            # append a new event instead of mutating history.
            ledger_path.write_text(lines_before[0].replace('promoted', 'forged'), encoding="utf-8")
            forged = controller.ledger.from_path(ledger_path, verify=False)
            self.assertFalse(forged.verify())

    def test_file_backed_ledger_fences_simultaneous_process_writers(self):
        """The JSONL append boundary is safe across independent processes."""

        worker = r'''
import json
from pathlib import Path
import sys
import time

from brain.evolution import PromotionLedger

ledger_path, ready_path, go_path, label = sys.argv[1:5]
ledger = PromotionLedger(path=ledger_path)
Path(ready_path).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15.0
while not Path(go_path).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
try:
    event = ledger.append(action=label, reason="cross-process-test")
    print(json.dumps({"ok": True, "sequence": event.sequence}), flush=True)
except Exception as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}), flush=True)
'''
        with tempfile.TemporaryDirectory(prefix="promotion-ledger-race-") as temp:
            root = Path(temp)
            ledger_path = root / "promotion.jsonl"
            go_path = root / "go"
            ready_paths = []
            processes = []
            for label in ("a", "b"):
                ready = root / f"ready-{label}"
                ready_paths.append(ready)
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker,
                            str(ledger_path),
                            str(ready),
                            str(go_path),
                            label,
                        ],
                        cwd=str(Path(__file__).resolve().parent),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            try:
                deadline = time.monotonic() + 15.0
                while not all(path.exists() for path in ready_paths):
                    if time.monotonic() >= deadline:
                        self.fail("promotion ledger workers did not become ready")
                    time.sleep(0.01)
                go_path.write_text("go", encoding="utf-8")
                outputs = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, stderr)
                    outputs.append(json.loads(stdout.strip()))
                self.assertEqual(sum(1 for item in outputs if item.get("ok")), 1)
                self.assertEqual(sum(1 for item in outputs if not item.get("ok")), 1)
                restored = PromotionLedger.from_path(ledger_path)
                self.assertTrue(restored.verify())
                self.assertEqual(len(restored.events), 1)
                self.assertEqual(restored.events[0].sequence, 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_file_backed_ledger_rejects_missing_history_before_recreating(self):
        with tempfile.TemporaryDirectory(prefix="promotion-ledger-missing-") as temp:
            path = Path(temp) / "promotion.jsonl"
            ledger = PromotionLedger(path=path)
            ledger.append(action="first")
            path.unlink()
            with self.assertRaises(LedgerError):
                ledger.append(action="second")
            self.assertFalse(path.exists())

    def test_controller_lock_is_scoped_to_active_root_not_ledger_path(self):
        """Separate receipt files must still serialize one active-tree swap."""

        with tempfile.TemporaryDirectory(prefix="promotion-controller-root-lock-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", quality="trusted")
            candidate_a = self._tree(root, "candidate-a", quality="better-a")
            candidate_b = self._tree(root, "candidate-b", quality="better-b")
            harness_a, receipt_a = self._receipt(root, active, candidate_a)
            harness_b, receipt_b = self._receipt(root, active, candidate_b)
            controller_a = PromotionController(
                active,
                harness=harness_a,
                protected_files=("protected.txt",),
                ledger_path=root / "ledger-a.jsonl",
                profile="legacy",
            )
            controller_b = PromotionController(
                active,
                harness=harness_b,
                protected_files=("protected.txt",),
                ledger_path=root / "ledger-b.jsonl",
                profile="legacy",
            )

            # If the controllers accidentally use different locks, both swap
            # windows overlap and one process can restore the other's backup.
            # With the active-root lock, only the first reaches this barrier;
            # the timeout then lets it proceed and the second safely records a
            # stale-baseline rejection after acquiring the same lock.
            barrier = threading.Barrier(2)
            for controller in (controller_a, controller_b):
                original = controller._swap_in

                def delayed_swap(stage, _original=original):
                    try:
                        barrier.wait(timeout=0.75)
                    except threading.BrokenBarrierError:
                        pass
                    time.sleep(0.05)
                    return _original(stage)

                controller._swap_in = delayed_swap

            outcomes = []
            errors = []

            def run(controller, candidate, receipt):
                try:
                    outcomes.append(
                        controller.promote(candidate, receipt, authorized=True)
                    )
                except Exception as exc:  # pragma: no cover - regression signal
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(controller_a, candidate_a, receipt_a)),
                threading.Thread(target=run, args=(controller_b, candidate_b, receipt_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sum(1 for outcome in outcomes if outcome.accepted), 1)
            self.assertEqual(sum(1 for outcome in outcomes if not outcome.accepted), 1)
            self.assertTrue(active.is_dir())
            self.assertEqual((active / "protected.txt").read_text(encoding="utf-8"), "trusted-anchor")
            self.assertTrue(controller_a.ledger.verify())
            self.assertTrue(controller_b.ledger.verify())
            controller_a.close()
            controller_b.close()

    def test_hardlink_ledger_alias_is_rejected(self):
        """Path-based sidecars cannot safely coordinate hard-linked names."""

        with tempfile.TemporaryDirectory(prefix="promotion-ledger-hardlink-") as temp:
            root = Path(temp)
            original_path = root / "ledger.jsonl"
            PromotionLedger(path=original_path).append(action="seed")
            alias_path = root / "ledger-alias.jsonl"
            try:
                os.link(original_path, alias_path)
            except (AttributeError, OSError) as exc:
                self.skipTest(f"hard links unavailable on this filesystem: {exc}")
            with self.assertRaises(LedgerError):
                PromotionLedger(path=alias_path)

    def test_promotion_metadata_rejects_credential_shaped_fields(self):
        ledger = PromotionLedger()
        with self.assertRaises(LedgerError):
            ledger.append(
                action="rejected",
                metadata={"nested": {"secret_key": "[REDACTED_TEST_VALUE]"}},
            )
        self.assertEqual(len(ledger.events), 0)

    def test_protected_directory_with_sensitive_descendant_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="promotion-protected-dir-") as temp:
            root = Path(temp)
            active = self._tree(root, "active")
            protected = active / "config"
            protected.mkdir()
            # The guard inspects names/metadata only; the value is never read.
            (protected / ".env").write_text("[REDACTED_TEST_VALUE]", encoding="utf-8")
            with self.assertRaises(ProtectedFileError):
                PromotionController(
                    active,
                    harness=self._fixture_harness(root),
                    protected_paths=("config",),
                    profile="legacy",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
