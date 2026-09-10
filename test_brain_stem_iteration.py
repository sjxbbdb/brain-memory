"""P3 BrainStem candidate-iteration boundary tests.

The tests exercise the explicit three-step seam.  A heartbeat tick never
enters this path; only the test host invokes registration, evaluation and
promotion.
"""

from __future__ import annotations

import json
import inspect
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from brain.brain_stem import BrainStem
from brain.evaluation_harness import (
    BaselineRevision,
    EvaluationHarness,
    EvaluationReceipt,
    directory_fingerprint,
)
from brain.evolution import PromotionController, SandboxAttestationError, SandboxAttestor
from brain.life_kernel import LifeKernel, LifecycleError, LifecycleState
from brain.motivation import (
    ChangeProposal,
    ImpulseEvent,
    IterationNeed,
    MotivationSourceAttestor,
    MotivationalPressure,
)
from brain.succession_runtime import SuccessionCoordinator, SuccessionRuntimeError
from storage.database import MemoryStore, StateStore, init_db


class BrainStemIterationTests(unittest.TestCase):
    def test_cognitive_dispatch_ingests_emotion_once_per_tick(self):
        """Dispatch output must not be applied again by the common emotion stage."""

        tick_source = inspect.getsource(BrainStem._tick)
        self.assertIn("self.cognitive_dispatch.dispatch(", tick_source)
        self.assertEqual(tick_source.count("self.emotional_spectrum.ingest_llm_emotion("), 1)

    def _activate(self, stem: BrainStem) -> None:
        """Move a test kernel through the explicit constitutional wake edge."""

        stem.life_kernel.transition(
            LifecycleState.BOOTSTRAPPING,
            "test host bootstrap",
            event_type="test_bootstrap_started",
        )
        stem.life_kernel.transition(
            LifecycleState.ACTIVE,
            "test host activation",
            event_type="test_bootstrap_completed",
        )

    def _tree(self, root: Path, name: str, marker: str) -> Path:
        path = root / name
        path.mkdir()
        (path / "program.txt").write_text(marker, encoding="utf-8")
        return path

    def _harness(self, root: Path) -> EvaluationHarness:
        fixtures = root / "judge-fixtures"
        fixtures.mkdir()
        payload = json.dumps(
            {"status": "pass", "verified": True, "metrics": {"quality": 2.0}}
        )
        (fixtures / "judge.py").write_text(
            f"print({payload!r})\n", encoding="utf-8"
        )
        return EvaluationHarness(
            fixtures=fixtures,
            command=[sys.executable, "{fixtures}/judge.py", "{candidate}"],
            primary_dimension="quality",
        )

    def _need(self) -> IterationNeed:
        return IterationNeed(
            motive="growth",
            pressure=0.8,
            trigger="persistent_pressure",
            reason="verified improvement opportunity",
            evidence=("verified gap",),
            source_labels=("host-test",),
            confidence=0.9,
        )

    def _proposal(self, stem: BrainStem, revision_id: str = "") -> ChangeProposal:
        return stem.register_iteration_proposal(
            self._need(),
            host=object(),
            title="Improve parser",
            scope="brain/parser.py",
            hypothesis="the candidate lowers parse failures",
            evidence=("reproducible fixture",),
            expected_benefits=("higher quality",),
            risks=("regression",),
            resource_budget={"cpu_seconds": 30},
            rollback_revision="baseline-1",
            baseline_revision="baseline-1",
            candidate_revision=revision_id,
        )

    def test_host_is_required_and_default_stem_does_not_bind_capabilities(self):
        stem = BrainStem()
        with self.assertRaises(PermissionError):
            stem.register_iteration_proposal(
                self._need(),
                host=None,
                title="x",
                scope="brain/parser.py",
                hypothesis="h",
                evidence=("e",),
                expected_benefits=("b",),
                risks=("r",),
                resource_budget={"cpu_seconds": 1},
                rollback_revision="r1",
                baseline_revision="b1",
            )
        status = stem.iteration_pipeline_snapshot()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["host_bound"])
        self.assertIsNone(stem.evaluation_harness)
        self.assertIsNone(stem.promotion_controller)

    def test_production_brain_stem_requires_host_bound_motivation_provenance(self):
        stem = BrainStem()
        # A caller-supplied ``source=verified`` label is only a signal and is
        # rejected by the production default until a host proof is attached.
        before = stem.motivation.total_accepted
        stem.record_impulse(
            "growth", intensity=1.0, source="verified", context="caller", now=0
        )
        self.assertEqual(stem.motivation.total_accepted, before)

        attestor = MotivationSourceAttestor(
            secret=b"brain-stem-motivation-test-secret", clock=lambda: 0.0
        )
        bound = BrainStem(motivation_source_attestor=attestor)
        base = ImpulseEvent.create(
            "growth",
            1.0,
            "sensor-a",
            context="host observation",
            source_kind="external",
            event_id="stem-attested-1",
            timestamp="0",
        )
        proof = attestor.issue_for_event(base, source_id="sensor-a", now=0.0)
        event = ImpulseEvent.create(
            "growth",
            1.0,
            "sensor-a",
            context="host observation",
            source_kind="external",
            event_id="stem-attested-1",
            timestamp="0",
            provenance=proof,
        )
        self.assertGreaterEqual(bound.record_impulse(event, now=0), 0.0)
        self.assertEqual(bound.motivation.total_accepted, 1)

    def test_injected_motivation_policy_cannot_disagree_with_host_binding(self):
        legacy = MotivationalPressure(decay_rate=0.0, clock=lambda: 0.0)
        with self.assertRaises(ValueError):
            BrainStem(motivation=legacy)
        explicit_legacy = BrainStem(
            motivation=legacy,
            motivation_profile="legacy",
        )
        self.assertFalse(explicit_legacy.motivation.require_source_attestation)

        with self.assertRaises(ValueError):
            BrainStem(
                motivation=legacy,
                motivation_require_source_attestation=True,
                motivation_profile="legacy",
            )

        attestor = MotivationSourceAttestor(
            secret=b"brain-stem-policy-mismatch-secret", clock=lambda: 0.0
        )
        strict = MotivationalPressure(
            decay_rate=0.0,
            clock=lambda: 0.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        with self.assertRaises(ValueError):
            BrainStem(
                motivation=strict,
                motivation_source_attestor=MotivationSourceAttestor(
                    secret=b"different-policy-secret", clock=lambda: 0.0
                ),
            )

    def test_production_motivation_cannot_be_replaced_after_construction(self):
        stem = BrainStem()
        stem.motivation = MotivationalPressure(
            decay_rate=0.0,
            clock=lambda: 0.0,
        )
        with self.assertRaises(PermissionError):
            stem.record_impulse(
                "growth", intensity=1.0, source="forged", now=0.0
            )

    def test_registration_and_evaluation_are_explicit_and_non_writing(self):
        with tempfile.TemporaryDirectory(prefix="brain-stem-p3-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", "trusted")
            candidate = self._tree(root, "candidate", "better")
            harness = self._harness(root)
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=(),
                profile="legacy",
            )
            before_active = directory_fingerprint(active)
            before_candidate = directory_fingerprint(candidate)
            stem = BrainStem(evaluation_harness=harness, promotion_controller=controller)
            proposal = stem.register_iteration_proposal(
                self._need(),
                host={"name": "test-host"},
                title="Improve parser",
                scope="brain/parser.py",
                hypothesis="the candidate lowers parse failures",
                evidence=("reproducible fixture",),
                expected_benefits=("higher quality",),
                risks=("regression",),
                resource_budget={"cpu_seconds": 30},
                rollback_revision="baseline-1",
                baseline_revision="baseline-1",
            )
            baseline = BaselineRevision.from_path(
                active,
                metrics={"quality": 1.0},
                revision_id="baseline-1",
                harness_version=harness.harness_version,
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = stem.evaluate_iteration_proposal(
                proposal,
                candidate,
                baseline,
                host={"name": "test-host"},
            )
            self.assertIsInstance(receipt, EvaluationReceipt)
            self.assertTrue(receipt.verify())
            self.assertEqual(directory_fingerprint(active), before_active)
            self.assertEqual(directory_fingerprint(candidate), before_candidate)
            self.assertEqual(len(controller.ledger.events), 0)
            status = stem.iteration_pipeline_snapshot()
            self.assertEqual(status["proposal_count"], 1)
            self.assertEqual(status["evaluated_count"], 1)
            self.assertFalse(status["host_bound"])

    def test_promotion_requires_explicit_host_and_production_attestation(self):
        with tempfile.TemporaryDirectory(prefix="brain-stem-p3-prod-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", "trusted")
            candidate = self._tree(root, "candidate", "better")
            harness = self._harness(root)
            attestor = SandboxAttestor(secret=b"brain-stem-p3-test-secret")
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=(),
                sandbox_attestor=attestor,
                ledger_path=root / "promotion-prod.jsonl",
                persistence_path=root / "brain-prod.sqlite",
            )
            stem = BrainStem(
                evaluation_harness=harness,
                promotion_controller=controller,
            )
            proposal = stem.register_iteration_proposal(
                self._need(),
                host={"authorized": True},
                title="Improve parser",
                scope="brain/parser.py",
                hypothesis="the candidate lowers parse failures",
                expected_benefits=("higher quality",),
                risks=("regression",),
                resource_budget={"cpu_seconds": 30},
                rollback_revision="baseline-1",
                baseline_revision="baseline-1",
            )
            baseline = BaselineRevision.from_path(
                active,
                metrics={"quality": 1.0},
                revision_id="baseline-1",
                harness_version=harness.harness_version,
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = stem.evaluate_iteration_proposal(
                proposal, candidate, baseline, host={"name": "sandbox"}
            )
            # Evaluation is safe before wake; promotion is not.
            self._activate(stem)
            with self.assertRaises(SandboxAttestationError):
                stem.promote_iteration_proposal(
                    proposal,
                    candidate,
                    receipt,
                    host={"authorized": True},
                )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")
            attestation = attestor.issue_for_receipt(receipt)
            outcome = stem.promote_iteration_proposal(
                proposal,
                candidate,
                receipt,
                host={"authorized": True},
                sandbox_attestation=attestation,
            )
            self.assertTrue(outcome.promoted)
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "better")
            self.assertTrue(stem.iteration_pipeline_snapshot()["last_outcome"]["attestation_verified"])

    def test_promotion_respects_lifecycle_and_homeostasis_quarantine(self):
        with tempfile.TemporaryDirectory(prefix="brain-stem-p3-gate-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", "trusted")
            candidate = self._tree(root, "candidate", "better")
            harness = self._harness(root)
            attestor = SandboxAttestor(secret=b"brain-stem-p3-gate-secret")
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=(),
                sandbox_attestor=attestor,
                ledger_path=root / "promotion-gate-prod.jsonl",
                persistence_path=root / "brain-gate-prod.sqlite",
            )
            stem = BrainStem(
                evaluation_harness=harness,
                promotion_controller=controller,
            )
            proposal = self._proposal(stem)
            baseline = BaselineRevision.from_path(
                active,
                metrics={"quality": 1.0},
                revision_id="baseline-1",
                harness_version=harness.harness_version,
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = stem.evaluate_iteration_proposal(
                proposal, candidate, baseline, host=object()
            )
            attestation = attestor.issue_for_receipt(receipt)
            with self.assertRaises(LifecycleError):
                stem.promote_iteration_proposal(
                    proposal,
                    candidate,
                    receipt,
                    host={"authorized": True},
                    sandbox_attestation=attestation,
                )
            self.assertEqual(stem.life_kernel.state, LifecycleState.CREATED)
            self._activate(stem)
            stem.observe_resources({"action_risk": 11.0}, tick=0, source="test")
            self.assertTrue(stem.homeostasis.quarantine_latched)
            with self.assertRaises(LifecycleError):
                stem.promote_iteration_proposal(
                    proposal,
                    candidate,
                    receipt,
                    host={"authorized": True},
                    sandbox_attestation=attestation,
                )
            self.assertEqual((active / "program.txt").read_text(encoding="utf-8"), "trusted")

    def test_snapshot_restores_evidence_but_never_host_or_candidate_path(self):
        with tempfile.TemporaryDirectory(prefix="brain-stem-p3-restore-") as temp:
            root = Path(temp)
            active = self._tree(root, "active", "trusted")
            candidate = self._tree(root, "candidate", "better")
            harness = self._harness(root)
            controller = PromotionController(
                active, harness=harness, protected_files=(), profile="legacy"
            )
            stem = BrainStem(evaluation_harness=harness, promotion_controller=controller)
            proposal = stem.register_iteration_proposal(
                self._need(),
                host=object(),
                title="Improve parser",
                scope="brain/parser.py",
                hypothesis="the candidate lowers parse failures",
                expected_benefits=("higher quality",),
                risks=("regression",),
                resource_budget={"cpu_seconds": 30},
                rollback_revision="baseline-1",
                baseline_revision="baseline-1",
            )
            baseline = BaselineRevision.from_path(
                active,
                metrics={"quality": 1.0},
                revision_id="baseline-1",
                harness_version=harness.harness_version,
                fixture_hash=harness.fixture_hash,
                evaluator_hash=harness.evaluator_hash,
            )
            receipt = stem.evaluate_iteration_proposal(
                proposal, candidate, baseline, host=object()
            )
            # A malformed host label that looks like a local path is redacted
            # before persistence rather than copied into the snapshot.
            path_label = str(candidate)
            self._proposal(stem, path_label)
            payload = {
                **stem.state.snapshot(),
                "iteration_pipeline": stem._iteration_snapshot_for_persistence(),
            }
            self.assertNotIn(path_label, json.dumps(payload, ensure_ascii=False))
            restored = BrainStem(
                evaluation_harness=harness, promotion_controller=controller
            )
            restored._restore_snapshot(payload)
            restored_status = restored.iteration_pipeline_snapshot()
            self.assertEqual(restored_status["proposal_count"], 2)
            self.assertEqual(restored_status["evaluated_count"], 1)
            self.assertFalse(restored_status["host_bound"])
            self.assertNotIn("source_path", json.dumps(restored_status))
            # An explicit host is still required after restart; no snapshot
            # field can silently authorize or bind it.
            with self.assertRaises(PermissionError):
                restored.evaluate_iteration_proposal(proposal, candidate, baseline, host=None)


class BrainStemLifeControlLeaseTests(unittest.IsolatedAsyncioTestCase):
    """Integration checks for the durable, single-owner lifecycle lease.

    The first test exercises the full BrainStem startup/sleep/wake path with
    two independent StateStore instances.  The second uses separate Python
    interpreters so a passing result cannot be explained by the in-process
    SuccessionCoordinator guard alone.
    """

    async def test_falsey_configured_state_store_still_persists_life_and_snapshot(self):
        """Adapter presence, rather than truthiness, controls durability."""

        class FalseyStateStore(StateStore):
            def __bool__(self):  # type: ignore[override]
                return False

        with tempfile.TemporaryDirectory(prefix="brain-stem-falsey-store-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = FalseyStateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory)
            try:
                await stem.start()
                self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
                self.assertTrue(await stem._snapshot_state())
                self.assertTrue(store.load_latest())
                self.assertTrue(
                    store.load_life_events(instance_id=stem.life_kernel.instance_id)
                )
            finally:
                await stem.stop()
                store.close()
                memory.close()

    async def test_legacy_adapter_requires_explicit_mode_before_any_replay_write(self):
        class LegacyAdapter:
            def __init__(self):
                self.events = []
                self.snapshots = []

            def __bool__(self):
                return False

            def load_latest(self):
                return None

            def load_life_events(self, **_kwargs):
                return list(self.events)

            def append_life_event(self, event):
                self.events.append(dict(event))
                return True

            def save(self, snapshot):
                self.snapshots.append(dict(snapshot))
                return True

        blocked_adapter = LegacyAdapter()
        blocked = BrainStem(blocked_adapter)
        with self.assertRaises(LifecycleError):
            await blocked.start()
        self.assertEqual(blocked_adapter.events, [])

        legacy_adapter = LegacyAdapter()
        legacy = BrainStem(legacy_adapter, life_control_mode="legacy")
        try:
            await legacy.start()
            self.assertEqual(legacy.life_kernel.state, LifecycleState.ACTIVE)
            self.assertTrue(legacy_adapter.events)
        finally:
            await legacy.stop()

        class PartialAdapter(LegacyAdapter):
            def claim_life_control(self, *args, **kwargs):
                return None

        partial_adapter = PartialAdapter()
        partial = BrainStem(partial_adapter, life_control_mode="legacy")
        with self.assertRaises(LifecycleError):
            await partial.start()
        self.assertEqual(partial_adapter.events, [])

    async def test_two_stems_same_persistent_lineage_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory(prefix="brain-stem-life-lease-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            first_store = StateStore(db_path)
            first_memory = MemoryStore(db_path)
            second_store = StateStore(db_path)
            second_memory = MemoryStore(db_path)
            first = BrainStem(first_store, first_memory)
            second = BrainStem(second_store, second_memory)
            try:
                await first.start()
                lineage_id = first.life_kernel.lineage_id
                instance_id = first.life_kernel.instance_id
                self.assertEqual(first.life_kernel.state, LifecycleState.ACTIVE)
                live = second_store.get_life_control(lineage_id)
                self.assertIsNotNone(live)
                assert live is not None
                self.assertEqual(live.instance_id, instance_id)
                self.assertEqual(live.owner_id, first._life_control_owner_id)

                # The second stem restores the same durable lineage, but its
                # distinct owner must not become ACTIVE while the first lease
                # is live.  This is the integration-level split-brain gate.
                with self.assertRaises(LifecycleError):
                    await second.start()
                self.assertIsNone(second._task)
                self.assertEqual(first.life_kernel.state, LifecycleState.ACTIVE)
                still_live = second_store.get_life_control(lineage_id)
                self.assertIsNotNone(still_live)
                assert still_live is not None
                self.assertEqual(still_live.owner_id, first._life_control_owner_id)

                # An orderly stop releases only the first owner's proof.  The
                # second independent stem can then restore and explicitly
                # acquire the same instance; no duplicate lineage is minted.
                await first.stop()
                self.assertIsNone(second_store.get_life_control(lineage_id))
                await second.start()
                self.assertEqual(second.life_kernel.state, LifecycleState.ACTIVE)
                self.assertEqual(second.life_kernel.lineage_id, lineage_id)
                self.assertEqual(second.life_kernel.instance_id, instance_id)
                await second.stop()
            finally:
                # Keep cleanup idempotent if an assertion fails midway.
                for stem in (second, first):
                    if stem._task is not None:
                        try:
                            await stem.stop()
                        except Exception:
                            pass
                for store in (first_store, second_store, first_memory, second_memory):
                    try:
                        store.close()
                    except Exception:
                        pass

    async def test_shared_kernel_second_stem_cannot_clear_first_local_guard(self):
        """An unbound wrapper must not tear down another wrapper's claim."""

        kernel = LifeKernel()
        first = BrainStem(life_kernel=kernel)
        second = BrainStem(life_kernel=kernel)
        try:
            await first.start()
            lineage_id = kernel.lineage_id
            before = SuccessionCoordinator._control_registry.get(lineage_id)
            self.assertIsNotNone(before)
            assert before is not None
            self.assertIs(before[1], kernel)
            self.assertIs(before[2], first._life_local_control_token)

            # ``second`` has never claimed this kernel.  Its startup cleanup
            # must not release ``first``'s owner-token entry, and the guarded
            # claim must reject the duplicate heartbeat.
            with self.assertRaises(LifecycleError):
                await second.start()
            after = SuccessionCoordinator._control_registry.get(lineage_id)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertIs(after[1], kernel)
            self.assertIs(after[2], first._life_local_control_token)
            self.assertIsNone(second._task)
            self.assertIsNotNone(first._task)
            self.assertFalse(first._task.done())

            # Once the true owner stops, the next wrapper may explicitly
            # acquire the same kernel; this checks the guard is not leaked.
            await first.stop()
            self.assertNotIn(lineage_id, SuccessionCoordinator._control_registry)
            await second.start()
            self.assertIsNotNone(second._task)
            self.assertFalse(second._task.done())
            await second.stop()
        finally:
            for stem in (second, first):
                if stem._task is not None:
                    try:
                        await stem.stop()
                    except Exception:
                        pass

    def test_separate_process_claims_have_exactly_one_winner(self):
        """SQLite BEGIN IMMEDIATE fences simultaneous claims across hosts."""

        worker = r"""
import json
from pathlib import Path
import sys
import time

from storage.database import StateStore

db_path, ready_path, go_path, ordinal = sys.argv[1:5]
Path(ready_path).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15.0
while not Path(go_path).exists() and time.monotonic() < deadline:
    time.sleep(0.005)
store = StateStore(db_path)
lease = store.claim_life_control(
    "lineage-cross-process-test",
    f"instance-{ordinal}",
    f"owner-{ordinal}",
    generation=0,
    ttl_sec=30.0,
    now=1000.0,
)
print(json.dumps({"won": lease is not None, "fencing": lease.fencing if lease else None}))
store.close()
"""
        with tempfile.TemporaryDirectory(prefix="brain-stem-cross-process-") as temp:
            root = Path(temp)
            db_path = str(root / "brain.sqlite")
            init_db(db_path)
            go_path = root / "go"
            processes = []
            ready_paths = []
            for ordinal in ("a", "b"):
                ready = root / f"ready-{ordinal}"
                ready_paths.append(ready)
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker,
                            db_path,
                            str(ready),
                            str(go_path),
                            ordinal,
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
                        self.fail("cross-process lease workers did not become ready")
                    time.sleep(0.01)
                go_path.touch()
                outputs = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=20.0)
                    self.assertEqual(
                        process.returncode,
                        0,
                        msg=f"worker failed: stdout={stdout!r}, stderr={stderr!r}",
                    )
                    lines = [line for line in stdout.splitlines() if line.strip()]
                    self.assertTrue(
                        lines,
                        msg=f"worker produced no result: stderr={stderr!r}",
                    )
                    outputs.append(json.loads(lines[-1]))
                self.assertEqual(sum(bool(item.get("won")) for item in outputs), 1)
                winners = [item for item in outputs if item.get("won")]
                self.assertEqual(winners[0].get("fencing"), 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
