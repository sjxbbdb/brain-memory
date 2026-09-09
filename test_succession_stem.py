"""BrainStem integration tests for gated disaster succession."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from brain.brain_stem import BrainStem
from brain.life_kernel import LifecycleError, LifecycleState
from brain.succession import Anchor, AnchorKind, AnchorSet, AnchorTrust, FailureAssessment, FailureClass
from storage.database import MemoryStore, StateStore, init_db


def _anchors(stem: BrainStem) -> AnchorSet:
    lineage = stem.life_kernel.lineage_id
    instance = stem.life_kernel.instance_id
    generation = stem.life_kernel.generation
    common = dict(
        source="trusted",
        verified=True,
        trust_level=AnchorTrust.CONSTITUTIONAL,
        evidence_refs=("genesis-proof",),
        created_at="2026-09-09T00:00:00+00:00",
    )
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


@dataclass(frozen=True)
class _AcceptedReceipt:
    receipt_hash: str = "a" * 64
    accepted: bool = True
    isolated: bool = True
    result_verified: bool = True
    hard_gates_passed: bool = True

    def verify(self) -> bool:
        return True


class BrainStemSuccessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_handover_is_persisted_and_start_is_gated(self):
        with tempfile.TemporaryDirectory(prefix="brain-succession-stem-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory, succession_profile="legacy")
            await stem.start()
            parent_id = stem.life_kernel.instance_id
            outcome = await stem.succeed_to_next_generation(
                failure=FailureAssessment(
                    failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                    reason="ledger corruption",
                    evidence_refs=("incident-1",),
                ),
                anchors=_anchors(stem),
            )
            self.assertEqual(outcome.parent.state, LifecycleState.DEAD)
            self.assertIs(stem.life_kernel, outcome.successor)
            self.assertEqual(stem.life_kernel.state, LifecycleState.CREATED)
            self.assertIsNone(stem._task)
            self.assertTrue(stem._successor_activation_required)
            self.assertTrue(store.verify_life_ledger(instance_id=parent_id))
            self.assertTrue(store.verify_life_ledger(instance_id=outcome.successor.instance_id))
            self.assertEqual(len(store.load_succession_records()), 1)
            with self.assertRaises(LifecycleError):
                await stem.start()

            try:
                await stem.authorize_successor(_AcceptedReceipt())
                self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
                self.assertFalse(stem._successor_activation_required)
                await stem.start()
                self.assertIsNotNone(stem._task)
                await stem.stop()
            finally:
                store.close()
                memory.close()

    async def test_restart_preserves_unactivated_successor_gate(self):
        with tempfile.TemporaryDirectory(prefix="brain-succession-restart-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory, succession_profile="legacy")
            await stem.start()
            await stem.succeed_to_next_generation(
                failure=FailureAssessment(
                    failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                    reason="integrity fault",
                ),
                anchors=_anchors(stem),
            )
            store.close()
            memory.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(
                restored_store, restored_memory, succession_profile="legacy"
            )
            with self.assertRaises(LifecycleError):
                await restored.start()
            # This is an intentional admission gate, not an integrity
            # corruption marker; the snapshot itself remains verifiable.
            self.assertFalse(restored._life_restore_blocked)
            self.assertTrue(restored.state.life.get("successor_activation_required"))
            restored_store.close()
            restored_memory.close()

    async def test_permanent_p5_tables_rebuild_boundary_without_brain_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="brain-succession-ledger-only-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory, succession_profile="legacy")
            await stem.start()
            await stem.succeed_to_next_generation(
                failure=FailureAssessment(
                    failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                    reason="ledger-only recovery",
                ),
                anchors=_anchors(stem),
            )
            store.close()
            memory.close()
            import sqlite3

            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM brain_state")
                conn.commit()
            finally:
                conn.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(
                restored_store, restored_memory, succession_profile="legacy"
            )
            try:
                with self.assertRaises(LifecycleError):
                    await restored.start()
                self.assertIsNotNone(restored.succession_coordinator)
                self.assertTrue(restored._successor_activation_required)
                await restored.authorize_successor(_AcceptedReceipt())
                await restored.start()
                self.assertEqual(restored.life_kernel.state, LifecycleState.ACTIVE)
                await restored.stop()
            finally:
                restored_store.close()
                restored_memory.close()

    async def test_drift_without_recovery_failure_keeps_parent_running(self):
        stem = BrainStem()
        await stem.start()
        before = stem.life_kernel.instance_id
        with self.assertRaises(Exception):
            await stem.succeed_to_next_generation(
                failure=FailureAssessment(
                    failure_class=FailureClass.METACOGNITIVE_DRIFT,
                    reason="suspected drift",
                    confirmed=True,
                    recovery_attempted=False,
                ),
                anchors=_anchors(stem),
            )
        self.assertEqual(stem.life_kernel.instance_id, before)
        self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
        await stem.stop()

    async def test_partial_durable_handover_blocks_restart_without_activation_proof(self):
        """A child genesis without P5 records can never auto-start after a crash."""

        class FailingAnchorStore(StateStore):
            def append_anchor_set(self, anchor_set):
                return False

        with tempfile.TemporaryDirectory(prefix="brain-succession-partial-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = FailingAnchorStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory, succession_profile="legacy")
            await stem.start()
            with self.assertRaises(Exception):
                await stem.succeed_to_next_generation(
                    failure=FailureAssessment(
                        failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                        reason="anchor sink outage",
                    ),
                    anchors=_anchors(stem),
                )
            # The parent was sealed before the failing sink, while no child is
            # exposed by the coordinator.  This is a safe-stop state.
            self.assertEqual(stem.life_kernel.state, LifecycleState.DEAD)
            store.close()
            memory.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(
                restored_store, restored_memory, succession_profile="legacy"
            )
            try:
                with self.assertRaises(LifecycleError):
                    await restored.start()
                self.assertTrue(restored._life_restore_blocked)
                self.assertIn("continuity", restored._life_restore_error)
            finally:
                restored_store.close()
                restored_memory.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
