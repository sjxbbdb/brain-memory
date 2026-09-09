"""Runtime succession coordinator contracts (no process or remote writes)."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
import unittest

from brain.life_kernel import LifeKernel, LifecycleState
from brain.succession import (
    Anchor,
    AnchorKind,
    AnchorSet,
    AnchorVault,
    AnchorTrust,
    FailureAssessment,
    FailureClass,
)
from brain.succession_runtime import SuccessionCoordinator, SuccessionRuntimeError
from brain.succession_runtime import SuccessorActivationAttestor
from brain.evaluation_harness import EvaluationReceipt, GateResult


def _anchors(lineage: str, instance: str, generation: int) -> AnchorSet:
    common = dict(source="trusted", verified=True, trust_level=AnchorTrust.CONSTITUTIONAL,
                  evidence_refs=("genesis-proof",), created_at="2026-09-08T00:00:00+00:00")
    return AnchorSet(
        lineage_id=lineage,
        generation=generation,
        instance_id=instance,
        anchors=(
            Anchor(AnchorKind.IDENTITY_ROOT, {"lineage_id": lineage}, anchor_id="identity", **common),
            Anchor(AnchorKind.CORE_PURPOSE, "活下去，并且活好", anchor_id="purpose", **common),
            Anchor(AnchorKind.LIFE_RULE, "audit", anchor_id="rule", **common),
            Anchor(AnchorKind.LINEAGE_METADATA, {"lineage_id": lineage}, anchor_id="lineage", **common),
        ),
    )


def _rehash_runtime_snapshot(payload: dict) -> dict:
    """Recompute the unkeyed envelope hash for adversarial fixture data."""

    raw = dict(payload)
    raw.pop("integrity_hash", None)
    payload["integrity_hash"] = hashlib.sha256(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return payload


class SuccessionCoordinatorTests(unittest.TestCase):
    def test_hard_fault_freezes_parent_and_creates_new_generation(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        anchors = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        failure = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="ledger mismatch",
            evidence_refs=("proof-1",),
            confirmed=True,
        )
        coordinator = SuccessionCoordinator(parent)
        outcome = coordinator.succeed(failure=failure, anchors=anchors)
        self.assertEqual(parent.state, LifecycleState.DEAD)
        self.assertEqual(outcome.successor.lineage_id, parent.lineage_id)
        self.assertEqual(outcome.successor.generation, parent.generation + 1)
        self.assertNotEqual(outcome.successor.instance_id, parent.instance_id)
        self.assertEqual(outcome.successor.parent_instance_id, parent.instance_id)
        self.assertEqual(outcome.successor.state, LifecycleState.CREATED)
        self.assertTrue(outcome.record.verify())
        self.assertEqual(coordinator.active_instance_id, outcome.successor.instance_id)

    def test_drift_requires_confirmation_and_recovery_failure(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        anchors = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        not_ready = FailureAssessment(
            failure_class=FailureClass.IDENTITY_DRIFT,
            reason="suspected drift",
            evidence_refs=("probe",),
            confirmed=True,
            recovery_attempted=False,
        )
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator(parent).succeed(failure=not_ready, anchors=anchors)
        self.assertEqual(parent.state, LifecycleState.ACTIVE)

    def test_snapshot_roundtrip_and_single_active_guard(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        anchors = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        failure = FailureAssessment(
            failure_class=FailureClass.METACOGNITIVE_DRIFT,
            reason="confirmed drift",
            evidence_refs=("p1", "p2"),
            confirmed=True,
            recovery_attempted=True,
            recovery_failed=True,
        )
        coordinator = SuccessionCoordinator(parent)
        outcome = coordinator.succeed(failure=failure, anchors=anchors)
        payload = json.loads(json.dumps(coordinator.snapshot(), ensure_ascii=False))
        restored = SuccessionCoordinator.from_snapshot(payload)
        self.assertEqual(restored.active_instance_id, outcome.successor.instance_id)
        self.assertTrue(restored.verify())
        with self.assertRaises(SuccessionRuntimeError):
            coordinator.succeed(failure=failure, anchors=anchors)

    def test_recoverable_incident_never_creates_a_generation(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        anchors = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        failure = FailureAssessment(
            failure_class=FailureClass.TRANSIENT,
            reason="temporary timeout",
        )
        coordinator = SuccessionCoordinator(parent)
        with self.assertRaises(SuccessionRuntimeError):
            coordinator.succeed(failure=failure, anchors=anchors)
        self.assertEqual(parent.state, LifecycleState.ACTIVE)
        self.assertIsNone(coordinator.successor)
        self.assertEqual(len(coordinator.succession_ledger.records), 0)

    def test_production_rejects_bare_or_duck_reevaluation_attestations(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        base = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        skill = Anchor(
            AnchorKind.SKILL,
            "candidate-skill",
            source="trusted",
            verified=True,
            trust_level=AnchorTrust.VERIFIED,
            evidence_refs=("skill-proof",),
            anchor_id="skill",
        )
        anchors = AnchorSet(
            base.lineage_id,
            base.generation,
            base.instance_id,
            anchors=base.anchors + (skill,),
        )
        failure = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="reevaluation gate",
        )
        coordinator = SuccessionCoordinator(parent)
        with self.assertRaises(SuccessionRuntimeError):
            coordinator.succeed(
                failure=failure,
                anchors=anchors,
                reevaluated_anchor_ids={"skill"},
            )

        @dataclass
        class DuckReceipt:
            accepted: bool = True
            isolated: bool = True
            result_verified: bool = True
            hard_gates_passed: bool = True
            receipt_hash: str = "a" * 64

            def verify(self):
                return True

        with self.assertRaises(SuccessionRuntimeError):
            coordinator.succeed(
                failure=failure,
                anchors=anchors,
                reevaluated_anchor_ids={"skill"},
                evaluation_receipts={"skill": DuckReceipt()},
            )
        self.assertEqual(parent.state, LifecycleState.ACTIVE)

    def test_forbidden_anchor_is_redacted_and_not_sealed(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        base = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        secret = Anchor(
            AnchorKind.CREDENTIAL,
            "must-not-cross",
            source="trusted",
            verified=True,
            trust_level=AnchorTrust.VERIFIED,
            evidence_refs=("secret-proof",),
            anchor_id="credential",
        )
        anchors = AnchorSet(
            base.lineage_id,
            base.generation,
            base.instance_id,
            anchors=base.anchors + (secret,),
        )
        failure = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="corrupt history",
        )
        outcome = SuccessionCoordinator(parent).succeed(
            failure=failure,
            anchors=anchors,
        )
        self.assertIn("credential", outcome.record.excluded_anchor_ids)
        self.assertNotIn("must-not-cross", json.dumps(outcome.record.to_dict()))
        self.assertNotIn("credential", outcome.successor_anchor_set.to_dict().get("anchors", []))

    def test_snapshot_top_level_tamper_is_rejected(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(parent)
        coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="tamper",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        payload = coordinator.snapshot()
        payload["active_instance_id"] = "attacker"
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator.from_snapshot(payload)

    def test_snapshot_cannot_rehash_itself_into_legacy_activation_policy(self):
        """The current host policy, not snapshot text, owns attestation mode."""

        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(parent)
        coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="policy tamper fixture",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        payload = coordinator.snapshot()
        payload["activation_profile"] = "legacy"
        payload["require_activation_attestation"] = False
        _rehash_runtime_snapshot(payload)
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator.from_snapshot(payload)

    def test_snapshot_rejects_anchor_set_from_another_lineage(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(parent)
        outcome = coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="tamper",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        wrong = _anchors(
            "unrelated-lineage",
            outcome.successor.instance_id,
            outcome.successor.generation,
        )
        vault = AnchorVault(anchor_sets=[outcome.successor_anchor_set, wrong])
        payload = coordinator.snapshot()
        payload["anchor_vault"] = vault.snapshot()
        payload["successor_anchor_set_id"] = wrong.anchor_set_id
        _rehash_runtime_snapshot(payload)
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator.from_snapshot(payload)

    def test_snapshot_rejects_anchor_set_with_wrong_generation_or_payload(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(parent)
        outcome = coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="tamper",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        base = outcome.successor_anchor_set
        for bad_set in (
            AnchorSet(
                lineage_id=base.lineage_id,
                generation=base.generation + 1,
                instance_id=base.instance_id,
                anchor_set_id="wrong-generation",
                anchors=base.anchors,
                metadata=dict(base.metadata),
            ),
            AnchorSet(
                lineage_id=base.lineage_id,
                generation=base.generation,
                instance_id=base.instance_id,
                anchor_set_id="wrong-payload",
                anchors=tuple(
                    Anchor(
                        anchor.kind,
                        "rewritten-purpose" if anchor.anchor_id == "purpose" else anchor.value,
                        source=anchor.source,
                        verified=anchor.verified,
                        trust_level=anchor.trust_level,
                        evidence_refs=anchor.evidence_refs,
                        anchor_id=anchor.anchor_id,
                        created_at=anchor.created_at,
                        metadata=dict(anchor.metadata),
                    )
                    for anchor in base.anchors
                ),
                metadata=dict(base.metadata),
            ),
        ):
            payload = coordinator.snapshot()
            payload["anchor_vault"] = AnchorVault(anchor_sets=[bad_set]).snapshot()
            payload["successor_anchor_set_id"] = bad_set.anchor_set_id
            _rehash_runtime_snapshot(payload)
            with self.subTest(anchor_set_id=bad_set.anchor_set_id):
                with self.assertRaises(SuccessionRuntimeError):
                    SuccessionCoordinator.from_snapshot(payload)

    def test_empty_vault_passed_by_host_is_mutated_in_place(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        vault = __import__("brain.succession", fromlist=["AnchorVault"]).AnchorVault()
        coordinator = SuccessionCoordinator(parent, anchor_vault=vault)
        coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="integrity",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        self.assertEqual(len(vault), 1)

    def test_two_coordinators_cannot_publish_two_successors(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        anchors = _anchors(parent.lineage_id, parent.instance_id, parent.generation)
        failure = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="concurrent fault",
        )
        coordinators = [SuccessionCoordinator(parent), SuccessionCoordinator(parent)]
        results = []
        errors = []

        def run(item):
            try:
                results.append(item.succeed(failure=failure, anchors=anchors))
            except Exception as exc:  # one loser is expected
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(item,)) for item in coordinators]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(parent.state, LifecycleState.DEAD)

    def test_successor_activation_requires_verified_receipt_and_preserves_single_active(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(parent, profile="legacy")
        outcome = coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="integrity",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )

        @dataclass(frozen=True)
        class Receipt:
            receipt_hash: str = "b" * 64
            accepted: bool = True
            isolated: bool = True
            result_verified: bool = True
            hard_gates_passed: bool = True

            def verify(self):
                return True

        child = coordinator.activate_successor(Receipt())
        self.assertEqual(child.state, LifecycleState.ACTIVE)
        self.assertEqual(len(coordinator.active_kernels), 1)
        self.assertTrue(coordinator.verify())
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator(parent).activate_successor(Receipt())

    def test_production_activation_attestation_binds_the_exact_successor_boundary(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        parent.transition(LifecycleState.ACTIVE, "ready")
        coordinator = SuccessionCoordinator(
            parent,
            activation_attestor=SuccessorActivationAttestor(
                secret=b"successor-attestor-secret-012345"
            ),
        )
        outcome = coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="integrity",
            ),
            anchors=_anchors(parent.lineage_id, parent.instance_id, parent.generation),
        )
        receipt = EvaluationReceipt(
            receipt_id="receipt-successor-1",
            harness_version="harness-test",
            fixture_hash="a" * 64,
            evaluator_hash="b" * 64,
            mode="evolution",
            candidate_revision_id="candidate-successor-1",
            candidate_fingerprint="c" * 64,
            baseline_revision_id="baseline-1",
            baseline_fingerprint="d" * 64,
            baseline_metrics={"quality": 1.0},
            candidate_metrics={"quality": 2.0},
            primary_dimension="quality",
            improvement=1.0,
            gates=(GateResult("independent", True),),
            accepted=True,
            outcome="ACCEPTED_EVOLUTION",
            rollback_performed=False,
            rollback_target=None,
            isolated=True,
            started_at="2026-09-09T00:00:00+00:00",
            finished_at="2026-09-09T00:00:01+00:00",
            duration_sec=1.0,
            exit_code=0,
            timed_out=False,
            stdout_bytes=0,
            stderr_bytes=0,
            result_verified=True,
        )
        with self.assertRaises(SuccessionRuntimeError):
            coordinator.activate_successor(receipt)
        proof = coordinator.activation_attestor.issue(outcome, receipt)
        child = coordinator.activate_successor(
            receipt, activation_attestation=proof
        )
        self.assertEqual(child.state, LifecycleState.ACTIVE)
        self.assertTrue(coordinator.verify())
        self.assertEqual(
            coordinator.activation_attestor.consumed_ids,
            frozenset({proof.attestation_id}),
        )
        # The signed activation proof is persisted in the runtime snapshot and
        # must be revalidated against the reconstructed child/outcome.
        payload = json.loads(json.dumps(coordinator.snapshot(), ensure_ascii=False))
        missing = dict(payload)
        missing["activation_attestation"] = None
        _rehash_runtime_snapshot(missing)
        SuccessionCoordinator.release_control(child)
        with self.assertRaises(SuccessionRuntimeError):
            SuccessionCoordinator.from_snapshot(
                missing, activation_attestor=coordinator.activation_attestor
            )
        restored = SuccessionCoordinator.from_snapshot(
            payload, activation_attestor=coordinator.activation_attestor
        )
        try:
            self.assertEqual(restored.successor.state, LifecycleState.ACTIVE)
            self.assertIsNotNone(restored.activation_attestation)
            self.assertTrue(restored.verify())
        finally:
            if restored.successor is not None:
                SuccessionCoordinator.release_control(restored.successor)
        # A valid receipt/proof pair cannot be grafted onto another child.
        other_parent = LifeKernel()
        other_parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
        other_parent.transition(LifecycleState.ACTIVE, "ready")
        other = SuccessionCoordinator(
            other_parent,
            activation_attestor=coordinator.activation_attestor,
        )
        other_outcome = other.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="another boundary",
            ),
            anchors=_anchors(
                other_parent.lineage_id,
                other_parent.instance_id,
                other_parent.generation,
            ),
        )
        # The proof is already consumed and, more importantly, names the
        # first record/child; either check must reject the graft.
        with self.assertRaises(SuccessionRuntimeError):
            other.activate_successor(receipt, activation_attestation=proof)


if __name__ == "__main__":
    unittest.main(verbosity=2)
