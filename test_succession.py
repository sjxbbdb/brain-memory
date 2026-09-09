"""P5 contracts for continuity anchors and bounded succession.

These tests deliberately exercise a data/policy boundary only.  Succession
must not start a process, revive a terminal ``LifeKernel``, or copy ambient
credentials.  Integration with the runtime remains a later, explicit step.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import unittest

from brain.succession import (
    Anchor,
    AnchorIntegrityError,
    AnchorKind,
    AnchorSet,
    AnchorTrust,
    AnchorVault,
    FailureAssessment,
    FailureClass,
    FailureDisposition,
    InheritanceDisposition,
    SuccessionError,
    SuccessionRecord,
    filter_inheritance,
)


LINEAGE = "lineage-contract-test"
PARENT = "instance-parent"


def _anchor(
    kind: AnchorKind,
    value,
    *,
    anchor_id: str,
    trust: AnchorTrust = AnchorTrust.VERIFIED,
    verified: bool = True,
    evidence=("receipt-1",),
) -> Anchor:
    return Anchor(
        kind=kind,
        value=value,
        anchor_id=anchor_id,
        source="trusted-observer",
        verified=verified,
        trust_level=trust,
        evidence_refs=evidence,
        created_at="2026-09-08T00:00:00+00:00",
    )


def _constitutional_anchors() -> list[Anchor]:
    return [
        _anchor(
            AnchorKind.IDENTITY_ROOT,
            {"lineage_id": LINEAGE},
            anchor_id="identity",
            trust=AnchorTrust.CONSTITUTIONAL,
        ),
        _anchor(
            AnchorKind.CORE_PURPOSE,
            "活下去，并且活好",
            anchor_id="purpose",
            trust=AnchorTrust.CONSTITUTIONAL,
        ),
        _anchor(
            AnchorKind.LIFE_RULE,
            "preserve truthful audit",
            anchor_id="life-rule",
            trust=AnchorTrust.CONSTITUTIONAL,
        ),
        _anchor(
            AnchorKind.LINEAGE_METADATA,
            {"lineage_id": LINEAGE, "generation": 3},
            anchor_id="lineage-meta",
            trust=AnchorTrust.CONSTITUTIONAL,
        ),
    ]


def _anchor_set(*extra: Anchor) -> AnchorSet:
    return AnchorSet(
        lineage_id=LINEAGE,
        generation=3,
        instance_id=PARENT,
        anchors=tuple(_constitutional_anchors()) + tuple(extra),
        anchor_set_id="anchors-g3",
        created_at="2026-09-08T00:01:00+00:00",
    )


class AnchorContractsTests(unittest.TestCase):
    def test_anchor_is_deeply_immutable_and_hash_pinned(self):
        anchor = _anchor(
            AnchorKind.LIFE_HISTORY,
            {"events": ["born", {"learned": True}]},
            anchor_id="history",
        )
        self.assertEqual(len(anchor.integrity_hash), 64)
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            anchor.verified = False  # type: ignore[misc]
        with self.assertRaises(TypeError):
            anchor.value["events"] = []  # type: ignore[index]
        with self.assertRaises(TypeError):
            anchor.value["events"][1]["learned"] = False  # type: ignore[index]

        restored = Anchor.from_dict(json.loads(json.dumps(anchor.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored.to_dict(), anchor.to_dict())
        tampered = anchor.to_dict()
        tampered["value"]["events"][0] = "rewritten"
        with self.assertRaises(AnchorIntegrityError):
            Anchor.from_dict(tampered)

    def test_anchor_set_round_trip_detects_rewrite_and_duplicate_ids(self):
        source = _anchor_set(
            _anchor(AnchorKind.LIFE_HISTORY, "verified episode", anchor_id="history")
        )
        self.assertTrue(source.verify())
        restored = AnchorSet.from_dict(
            json.loads(json.dumps(source.to_dict(), ensure_ascii=False))
        )
        self.assertEqual(restored.to_dict(), source.to_dict())

        rewritten = source.to_dict()
        rewritten["generation"] = 4
        with self.assertRaises(AnchorIntegrityError):
            AnchorSet.from_dict(rewritten)

        duplicate = _constitutional_anchors()
        duplicate.append(duplicate[0])
        with self.assertRaises(AnchorIntegrityError):
            AnchorSet(
                lineage_id=LINEAGE,
                generation=3,
                instance_id=PARENT,
                anchors=duplicate,
            )

    def test_anchor_vault_is_append_only_hash_chained_and_fail_closed(self):
        first = _anchor_set()
        second = AnchorSet(
            lineage_id=LINEAGE,
            generation=4,
            instance_id="instance-successor",
            anchors=first.anchors,
            anchor_set_id="anchors-g4",
            created_at="2026-09-08T00:02:00+00:00",
        )
        vault = AnchorVault()
        vault.append(first)
        vault.put(second)
        self.assertEqual(vault.latest(LINEAGE), second)
        self.assertTrue(vault.verify_chain())
        self.assertNotIn("delete", dir(vault))

        restored = AnchorVault.from_snapshot(
            json.loads(json.dumps(vault.snapshot(), ensure_ascii=False))
        )
        self.assertEqual(restored.latest(LINEAGE), second)
        self.assertTrue(restored.verify_chain())
        with self.assertRaises(AnchorIntegrityError):
            vault.append(second)

        corrupted = vault.snapshot()
        corrupted["entries"][0]["anchor_set"]["instance_id"] = "rewritten"
        with self.assertRaises(AnchorIntegrityError):
            AnchorVault.from_snapshot(corrupted)

    def test_vault_refuses_secrets_and_unverified_or_incomplete_sets(self):
        secret = _anchor(
            AnchorKind.CREDENTIAL,
            "API-SECRET-MUST-NOT-PERSIST",
            anchor_id="credential",
        )
        with self.assertRaises(AnchorIntegrityError):
            AnchorVault().append(_anchor_set(secret))

        unverified = _anchor(
            AnchorKind.LIFE_HISTORY,
            "rumor",
            anchor_id="rumor",
            trust=AnchorTrust.UNVERIFIED,
            verified=False,
            evidence=(),
        )
        with self.assertRaises(AnchorIntegrityError):
            AnchorVault().append(_anchor_set(unverified))

        with self.assertRaises(AnchorIntegrityError):
            AnchorVault().append(
                AnchorSet(
                    lineage_id=LINEAGE,
                    generation=3,
                    instance_id=PARENT,
                    anchors=(_constitutional_anchors()[0],),
                )
            )


class InheritanceContractsTests(unittest.TestCase):
    def test_filter_enforces_constitutional_history_and_reevaluation_rules(self):
        history = _anchor(
            AnchorKind.LIFE_HISTORY,
            "verified life episode",
            anchor_id="history",
        )
        memory = _anchor(
            AnchorKind.MEMORY,
            "verified memory",
            anchor_id="memory",
        )
        skill = _anchor(AnchorKind.SKILL, "parser-v2", anchor_id="skill")
        strategy = _anchor(AnchorKind.STRATEGY, "retry-plan", anchor_id="strategy")
        transient = _anchor(
            AnchorKind.AFFECT_STATE,
            "panic",
            anchor_id="affect",
        )
        plan = filter_inheritance(
            _anchor_set(history, memory, skill, strategy, transient),
            successor_generation=4,
        )

        inherited = set(plan.inherited_anchor_ids)
        self.assertTrue({"identity", "purpose", "life-rule", "lineage-meta"} <= inherited)
        self.assertTrue({"history", "memory"} <= inherited)
        self.assertEqual(set(plan.reevaluation_anchor_ids), {"skill", "strategy"})
        self.assertIn("affect", plan.excluded_anchor_ids)
        self.assertEqual(
            plan.decision_for("affect").disposition,
            InheritanceDisposition.EXCLUDED,
        )
        self.assertTrue(plan.ready)

    def test_independently_reevaluated_skill_can_be_inherited(self):
        skill = _anchor(AnchorKind.SKILL, "parser-v2", anchor_id="skill")
        receipt = {
            "accepted": True,
            "receipt_id": "host-eval-skill",
            "receipt_hash": "a" * 64,
            "verify": lambda: True,
        }
        plan = filter_inheritance(
            _anchor_set(skill),
            successor_generation=4,
            reevaluated_anchor_ids={"skill"},
            evaluation_receipts={"skill": receipt},
        )
        self.assertIn("skill", plan.inherited_anchor_ids)
        self.assertEqual(
            plan.decision_for("skill").disposition,
            InheritanceDisposition.INHERITED_AFTER_REEVALUATION,
        )
        successor = plan.build_successor_anchor_set("instance-successor")
        self.assertEqual(successor.lineage_id, LINEAGE)
        self.assertEqual(successor.generation, 4)
        self.assertEqual(successor.instance_id, "instance-successor")

    def test_bare_reevaluation_id_is_rejected_without_explicit_legacy_opt_in(self):
        skill = _anchor(AnchorKind.SKILL, "parser-v2", anchor_id="skill")
        with self.assertRaises(SuccessionError):
            filter_inheritance(
                _anchor_set(skill),
                successor_generation=4,
                reevaluated_anchor_ids={"skill"},
            )

    def test_secret_task_session_and_unconfirmed_action_never_cross_boundary(self):
        forbidden = [
            _anchor(AnchorKind.CREDENTIAL, "secret-value", anchor_id="credential"),
            _anchor(AnchorKind.APPROVAL_TOKEN, "approve-all", anchor_id="approval"),
            _anchor(AnchorKind.EXTERNAL_SESSION, "cookie", anchor_id="session"),
            _anchor(AnchorKind.ACTIVE_TASK, "unfinished write", anchor_id="task"),
            _anchor(AnchorKind.UNCONFIRMED_ACTION, "push remote", anchor_id="action"),
            _anchor(AnchorKind.AFFECT_STATE, "anger", anchor_id="affect"),
        ]
        plan = filter_inheritance(
            _anchor_set(*forbidden),
            successor_generation=4,
            reevaluated_anchor_ids={item.anchor_id for item in forbidden},
        )
        self.assertTrue({item.anchor_id for item in forbidden} <= set(plan.excluded_anchor_ids))
        serialized = json.dumps(plan.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("approve-all", serialized)
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("push remote", serialized)

    def test_missing_or_untrusted_constitutional_anchor_fails_closed(self):
        partial = AnchorSet(
            lineage_id=LINEAGE,
            generation=3,
            instance_id=PARENT,
            anchors=tuple(_constitutional_anchors()[:-1]),
        )
        with self.assertRaises(AnchorIntegrityError):
            filter_inheritance(partial, successor_generation=4)

    def test_verified_history_and_memory_with_embedded_secret_or_path_are_excluded(self):
        sensitive_history = _anchor(
            AnchorKind.LIFE_HISTORY,
            {"episode": "learned", "metadata": {"access_token": "token-never-crosses"}},
            anchor_id="sensitive-history",
        )
        sensitive_memory = _anchor(
            AnchorKind.MEMORY,
            r"C:\Users\24763\Desktop\private-memory.txt",
            anchor_id="sensitive-memory",
        )
        plan = filter_inheritance(
            _anchor_set(sensitive_history, sensitive_memory), successor_generation=4
        )
        self.assertNotIn("sensitive-history", plan.inherited_anchor_ids)
        self.assertNotIn("sensitive-memory", plan.inherited_anchor_ids)
        self.assertEqual(
            plan.decision_for("sensitive-history").disposition,
            InheritanceDisposition.EXCLUDED,
        )
        self.assertEqual(
            plan.decision_for("sensitive-memory").disposition,
            InheritanceDisposition.EXCLUDED,
        )
        successor = plan.build_successor_anchor_set("instance-successor")
        serialized = json.dumps(successor.to_dict(), ensure_ascii=False)
        self.assertNotIn("token-never-crosses", serialized)
        self.assertNotIn("private-memory.txt", serialized)


class FailureAndSuccessionRecordTests(unittest.TestCase):
    def test_failure_policy_separates_recovery_from_succession(self):
        transient = FailureAssessment(
            failure_class=FailureClass.TRANSIENT,
            reason="temporary model timeout",
            evidence_refs=("health-1",),
            confirmed=True,
        )
        self.assertFalse(transient.requires_succession)
        self.assertEqual(transient.disposition, FailureDisposition.RECOVERY)

        drift_before_recovery = FailureAssessment(
            failure_class=FailureClass.IDENTITY_DRIFT,
            reason="identity mismatch",
            evidence_refs=("probe-1", "probe-2"),
            confirmed=True,
            recovery_failed=False,
        )
        self.assertFalse(drift_before_recovery.requires_succession)
        self.assertEqual(drift_before_recovery.disposition, FailureDisposition.QUARANTINE)

        drift_after_recovery = FailureAssessment(
            failure_class=FailureClass.METACOGNITIVE_DRIFT,
            reason="persistent evaluator mismatch",
            evidence_refs=("probe-1", "probe-2"),
            confirmed=True,
            recovery_attempted=True,
            recovery_failed=True,
        )
        self.assertTrue(drift_after_recovery.requires_succession)
        self.assertEqual(drift_after_recovery.disposition, FailureDisposition.SUCCESSION)

        hard = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="constitutional ledger mismatch",
            evidence_refs=("ledger-proof",),
            confirmed=True,
        )
        self.assertTrue(hard.requires_succession)

    def test_succession_record_is_immutable_redacted_and_tamper_evident(self):
        excluded_secret = _anchor(
            AnchorKind.CREDENTIAL,
            "DO-NOT-COPY",
            anchor_id="credential",
        )
        plan = filter_inheritance(
            _anchor_set(excluded_secret),
            successor_generation=4,
        )
        failure = FailureAssessment(
            failure_class=FailureClass.IDENTITY_DRIFT,
            reason="confirmed drift after rollback",
            evidence_refs=("probe-1", "probe-2"),
            confirmed=True,
            recovery_attempted=True,
            recovery_failed=True,
            incident_id="incident-1",
            detected_at="2026-09-08T00:03:00+00:00",
        )
        record = SuccessionRecord.create(
            lineage_id=LINEAGE,
            parent_instance_id=PARENT,
            parent_generation=3,
            successor_instance_id="instance-successor",
            failure=failure,
            inheritance=plan,
            parent_frozen_at="2026-09-08T00:04:00+00:00",
            created_at="2026-09-08T00:05:00+00:00",
        )
        self.assertEqual(record.successor_generation, 4)
        self.assertTrue(record.parent_frozen)
        self.assertIn("credential", record.excluded_anchor_ids)
        self.assertTrue(record.verify())
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            record.parent_frozen = False  # type: ignore[misc]

        payload = record.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("DO-NOT-COPY", encoded)
        restored = SuccessionRecord.from_dict(json.loads(encoded))
        self.assertEqual(restored.to_dict(), record.to_dict())

        payload["successor_generation"] = 9
        with self.assertRaises(SuccessionError):
            SuccessionRecord.from_dict(payload)

    def test_record_rejects_unqualified_failure_and_invalid_lineage_relation(self):
        plan = filter_inheritance(_anchor_set(), successor_generation=4)
        recoverable = FailureAssessment(
            failure_class=FailureClass.CAPABILITY_FAILURE,
            reason="optional skill unavailable",
            evidence_refs=("health-1",),
            confirmed=True,
            recovery_attempted=True,
            recovery_failed=True,
        )
        with self.assertRaises(SuccessionError):
            SuccessionRecord.create(
                lineage_id=LINEAGE,
                parent_instance_id=PARENT,
                parent_generation=3,
                successor_instance_id="instance-successor",
                failure=recoverable,
                inheritance=plan,
            )
        hard = FailureAssessment(
            failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
            reason="hash mismatch",
            evidence_refs=("proof",),
            confirmed=True,
        )
        with self.assertRaises(SuccessionError):
            SuccessionRecord.create(
                lineage_id="other-lineage",
                parent_instance_id=PARENT,
                parent_generation=3,
                successor_instance_id="instance-successor",
                failure=hard,
                inheritance=plan,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
