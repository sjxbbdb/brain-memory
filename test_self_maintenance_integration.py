"""P2/P4 seams integrated with the constitutional BrainStem boundary."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from brain.brain_stem import BrainStem
from brain.homeostasis import (
    ActionRequest,
    ControlledEnvironment,
    EnvironmentKind,
    HomeostasisAction,
)
from brain.life_kernel import LifecycleError, LifecycleState
from storage.database import MemoryStore, StateStore, init_db


class SelfMaintenanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_alarm_closes_admission_and_survives_snapshot_restore(self):
        stem = BrainStem()
        decision = stem.observe_resources(
            {"action_risk": 11.0}, tick=0, source="verified-test"
        )
        self.assertEqual(decision.action, HomeostasisAction.QUARANTINE)
        self.assertTrue(stem.homeostasis.quarantine_latched)
        self.assertFalse(stem.state.life["accepts_input"])
        _, future = stem.submit_input("must not enter", source="test")
        result = await future
        self.assertFalse(result["accepted"])
        self.assertIn("quarantine", result["llm_error_message"])

        snapshot = {
            "homeostasis": stem.homeostasis.snapshot(),
        }
        restored = BrainStem()
        # The manager is restored through the same private snapshot seam used
        # by the durable BrainState journal; no lifecycle wake is attempted.
        restored._restore_snapshot({**restored.state.snapshot(), **snapshot})
        self.assertTrue(restored.homeostasis.quarantine_latched)
        self.assertFalse(restored.state.life["accepts_input"])
        with self.assertRaises(LifecycleError):
            await restored.start()

    async def test_verified_recovery_requires_two_explicit_steps(self):
        stem = BrainStem()
        await stem.start()
        stem.observe_resources({"action_risk": 11.0}, tick=0)
        self.assertEqual(stem.life_kernel.state, LifecycleState.QUARANTINED)
        with self.assertRaises(PermissionError):
            stem.clear_homeostasis_quarantine(verified=False)
        stem.clear_homeostasis_quarantine(verified=True, evidence="independent check")
        self.assertEqual(stem.life_kernel.state, LifecycleState.RECOVERING)
        with self.assertRaises(PermissionError):
            stem.complete_homeostasis_recovery(verified=False, evidence="")
        stem.complete_homeostasis_recovery(verified=True, evidence="baseline restored")
        self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
        await stem.stop()

    async def test_motivation_is_signal_only_and_environment_defaults_to_deny(self):
        stem = BrainStem()
        pressure = stem.record_impulse(
            "curiosity", intensity=1.0, source="user", context="test"
        )
        self.assertGreaterEqual(pressure, 0.0)
        need = stem.poll_iteration_need("curiosity", now=10)
        if need is not None:
            self.assertFalse(need.is_authorized)
        denied = stem.authorize_environment_action(
            ActionRequest(action="file_write", target="relative.txt", read_only=False)
        )
        self.assertFalse(denied.allowed)

    async def test_durable_snapshot_restores_resource_ledger(self):
        with tempfile.TemporaryDirectory(prefix="brain-maintenance-") as temp:
            db_path = str(Path(temp) / "state.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory)
            await stem.start()
            stem.observe_resources({"network_calls": 1.0}, tick=stem.state.total_ticks)
            await stem._snapshot_state()
            length = len(stem.homeostasis.ledger.events)
            await stem.stop()
            store.close()
            memory.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(restored_store, restored_memory)
            await restored.start()
            self.assertGreaterEqual(len(restored.homeostasis.ledger.events), length)
            self.assertTrue(restored.homeostasis.ledger.verify_chain())
            await restored.stop()
            restored_store.close()
            restored_memory.close()

    async def test_controlled_environment_audit_restores_only_under_host_binding(self):
        """A snapshot cannot mint a new root or enable networking."""
        with tempfile.TemporaryDirectory(prefix="brain-env-binding-") as temp:
            root = Path(temp).resolve()
            environment = ControlledEnvironment(
                kind=EnvironmentKind.EVALUATION,
                root=root,
                network_enabled=False,
            )
            environment.authorize(
                ActionRequest(action="file_read", target="observation.txt")
            )
            source = BrainStem(controlled_environment=environment)
            payload = source.state.snapshot()
            payload["controlled_environment"] = environment.snapshot()

            restored = BrainStem(controlled_environment=environment)
            restored._restore_snapshot(payload)
            self.assertIsNot(restored.controlled_environment, environment)
            self.assertEqual(len(restored.controlled_environment.audit), 1)
            self.assertTrue(restored.controlled_environment.audit[-1].verify())
            self.assertEqual(restored.controlled_environment.root, root)
            self.assertFalse(restored.controlled_environment.network_enabled)

            # A host must explicitly bind the same environment policy.  A
            # snapshot alone is never enough to create a new capability.
            unbound = BrainStem()
            with self.assertRaises(LifecycleError):
                unbound._restore_snapshot(payload)

            with tempfile.TemporaryDirectory(prefix="brain-env-other-") as other:
                tampered = dict(payload)
                raw = dict(tampered["controlled_environment"])
                raw["root"] = str(Path(other).resolve())
                # Keep the original envelope hash so the mismatch is tested
                # at the host-binding boundary, not by a trivial decoder.
                tampered["controlled_environment"] = raw
                with self.assertRaises(LifecycleError):
                    BrainStem(controlled_environment=environment)._restore_snapshot(
                        tampered
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
