"""P4 contracts for bounded resources and controlled environment access."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from brain.homeostasis import (
    ActionRequest,
    ControlledEnvironment,
    EnvironmentKind,
    HomeostasisAction,
    HomeostasisController,
    ResourceBudget,
    ResourceLedger,
    ResourceObservation,
)


class HomeostasisContractTests(unittest.TestCase):
    def setUp(self):
        self.budget = ResourceBudget(
            limits={
                "compute_ms": 100.0,
                "memory_mb": 100.0,
                "storage_mb": 100.0,
                "network_calls": 10.0,
                "model_tokens": 1000.0,
                "action_risk": 10.0,
                "attention_load": 1.0,
            },
            warning_ratio=0.7,
            critical_ratio=0.9,
            window_ticks=10,
        )

    def test_budget_is_immutable_and_fingerprinted(self):
        self.assertEqual(len(self.budget.fingerprint), 64)
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            self.budget.warning_ratio = 0.1
        with self.assertRaises(TypeError):
            self.budget.limits["compute_ms"] = 1000000

    def test_warning_throttles_and_hard_action_risk_quarantines(self):
        controller = HomeostasisController(self.budget)
        low = controller.observe(
            ResourceObservation(tick=1, usage={"compute_ms": 10.0})
        )
        self.assertEqual(low.action, HomeostasisAction.CONTINUE)
        warning = controller.observe(
            ResourceObservation(tick=2, usage={"compute_ms": 65.0})
        )
        self.assertEqual(warning.action, HomeostasisAction.THROTTLE)
        hard = controller.observe(
            ResourceObservation(tick=3, usage={"action_risk": 11.0})
        )
        self.assertEqual(hard.action, HomeostasisAction.QUARANTINE)
        self.assertTrue(controller.quarantine_latched)

        # Low usage cannot silently clear a constitutional quarantine.
        later = controller.observe(
            ResourceObservation(tick=4, usage={"compute_ms": 0.0})
        )
        self.assertEqual(later.action, HomeostasisAction.QUARANTINE)

    def test_window_reset_prevents_unbounded_accumulation(self):
        controller = HomeostasisController(self.budget)
        first = controller.observe(
            ResourceObservation(tick=1, usage={"network_calls": 4.0})
        )
        second = controller.observe(
            ResourceObservation(tick=2, usage={"network_calls": 4.0})
        )
        self.assertEqual(first.action, HomeostasisAction.CONTINUE)
        self.assertEqual(second.action, HomeostasisAction.THROTTLE)
        reset = controller.observe(
            ResourceObservation(tick=12, usage={"network_calls": 1.0})
        )
        self.assertEqual(reset.action, HomeostasisAction.CONTINUE)
        self.assertEqual(controller.usage["network_calls"], 1.0)

    def test_critical_compute_sleeps_while_state_pressure_degrades(self):
        compute = HomeostasisController(self.budget).observe(
            ResourceObservation(tick=1, usage={"compute_ms": 90.0})
        )
        memory = HomeostasisController(self.budget).observe(
            ResourceObservation(tick=1, usage={"memory_mb": 90.0})
        )
        self.assertEqual(compute.action, HomeostasisAction.SLEEP)
        self.assertEqual(memory.action, HomeostasisAction.DEGRADE)

    def test_ledger_round_trip_and_tamper_detection(self):
        controller = HomeostasisController(self.budget)
        controller.observe(ResourceObservation(tick=1, usage={"compute_ms": 5.0}))
        controller.observe(ResourceObservation(tick=2, usage={"memory_mb": 25.0}))
        payload = json.loads(json.dumps(controller.snapshot(), ensure_ascii=False))
        restored = HomeostasisController.from_snapshot(payload)
        self.assertTrue(restored.ledger.verify_chain())
        self.assertEqual(restored.public_snapshot(), controller.public_snapshot())

        ledger_payload = payload["ledger"]
        ledger_payload["events"][0]["decision"]["reason"] = "tampered"
        with self.assertRaises(ValueError):
            ResourceLedger.from_snapshot(ledger_payload)

    def test_additive_overflow_is_rejected_atomically(self):
        """Finite samples must never leave an unjournaled inf/NaN usage."""
        controller = HomeostasisController(
            ResourceBudget(limits={"compute_ms": 300_000.0})
        )
        controller.observe(
            ResourceObservation(tick=1, usage={"compute_ms": 1e308})
        )
        before_usage = dict(controller.usage)
        before_head = controller.ledger.head_hash
        with self.assertRaises(ValueError):
            controller.observe(
                ResourceObservation(tick=2, usage={"compute_ms": 1e308})
            )
        self.assertEqual(dict(controller.usage), before_usage)
        self.assertEqual(controller.ledger.head_hash, before_head)
        self.assertEqual(controller.last_decision.tick, 1)
        self.assertTrue(all(math.isfinite(value) for value in controller.usage.values()))

    def test_ledger_capacity_quarantines_once_and_stays_bounded(self):
        """A long-lived loop must not spin on capacity exceptions."""
        controller = HomeostasisController(
            ResourceBudget(limits={"compute_ms": 100.0}),
            max_events=3,
        )
        first = controller.observe(
            ResourceObservation(tick=1, usage={"compute_ms": 1.0})
        )
        second = controller.observe(
            ResourceObservation(tick=2, usage={"compute_ms": 1.0})
        )
        terminal = controller.observe(
            ResourceObservation(tick=3, usage={"compute_ms": 1.0})
        )
        self.assertEqual(first.action, HomeostasisAction.CONTINUE)
        self.assertEqual(second.action, HomeostasisAction.CONTINUE)
        self.assertEqual(terminal.action, HomeostasisAction.QUARANTINE)
        self.assertIn("ledger_events", terminal.exceeded)
        self.assertTrue(controller.quarantine_latched)
        self.assertEqual(len(controller.ledger.events), 3)

        # Subsequent heartbeats receive the same safe decision without trying
        # to append beyond the cap or emitting an exception.
        repeat = controller.observe(
            ResourceObservation(tick=4, usage={"compute_ms": 1.0})
        )
        self.assertEqual(repeat.action, HomeostasisAction.QUARANTINE)
        self.assertEqual(repeat, terminal)
        self.assertEqual(len(controller.ledger.events), 3)

        restored = HomeostasisController.from_snapshot(controller.snapshot())
        self.assertTrue(restored.quarantine_latched)
        self.assertEqual(len(restored.ledger.events), 3)

    def test_snapshot_rejects_non_finite_usage_projection(self):
        controller = HomeostasisController(self.budget)
        # Simulate a corrupted in-memory adapter without allowing the bad
        # value to escape through a snapshot boundary.
        controller._usage["compute_ms"] = float("inf")
        with self.assertRaises(ValueError):
            controller.snapshot()
        with self.assertRaises(ValueError):
            controller.public_snapshot()


class ControlledEnvironmentTests(unittest.TestCase):
    def test_evaluation_world_isolated_from_network_and_outside_writes(self):
        with tempfile.TemporaryDirectory(prefix="brain-eval-world-") as temp_dir:
            root = Path(temp_dir).resolve()
            env = ControlledEnvironment(
                kind=EnvironmentKind.EVALUATION,
                root=root,
                network_enabled=False,
            )
            inside = env.authorize(ActionRequest(
                action="file_read", target=str(root / "fixture.txt"), read_only=True
            ))
            self.assertTrue(inside.allowed)
            relative = env.authorize(ActionRequest(
                action="file_read", target="relative-fixture.txt", read_only=True
            ))
            self.assertTrue(relative.allowed)
            outside = env.authorize(ActionRequest(
                action="file_write", target=str(root.parent / "outside.txt"), read_only=False
            ))
            self.assertFalse(outside.allowed)
            network = env.authorize(ActionRequest(
                action="network_get", target="https://example.com", read_only=True,
                uses_network=True,
            ))
            self.assertFalse(network.allowed)

    def test_living_world_write_requires_bound_approval(self):
        with tempfile.TemporaryDirectory(prefix="brain-living-world-") as temp_dir:
            root = Path(temp_dir).resolve()

            def approve(request, token):
                return token == "approved" and request.target.endswith("allowed.txt")

            env = ControlledEnvironment(
                kind=EnvironmentKind.LIVING,
                root=root,
                network_enabled=True,
                approval_validator=approve,
            )
            denied = env.authorize(ActionRequest(
                action="file_write", target=str(root / "allowed.txt"), read_only=False
            ))
            self.assertFalse(denied.allowed)
            allowed = env.authorize(ActionRequest(
                action="file_write", target=str(root / "allowed.txt"), read_only=False,
                approval_token="approved",
            ))
            self.assertTrue(allowed.allowed)
            self.assertEqual(env.audit[-1].request_hash, allowed.request_hash)

    def test_environment_audit_roundtrip_verifies_request_and_chain(self):
        with tempfile.TemporaryDirectory(prefix="brain-audit-") as temp_dir:
            env = ControlledEnvironment(kind=EnvironmentKind.EVALUATION, root=temp_dir)
            env.authorize(ActionRequest(action="file_read", target="one.txt"))
            env.authorize(ActionRequest(action="file_read", target="two.txt"))
            payload = env.snapshot()
            restored = ControlledEnvironment.from_snapshot(payload, expected_root=temp_dir)
            self.assertEqual(len(restored.audit), 2)
            self.assertEqual(restored.audit[-1].previous_hash, restored.audit[0].audit_hash)
            self.assertTrue(restored.audit[0].verify())
            self.assertTrue(restored.audit[1].verify(restored.audit[0].audit_hash))

    def test_environment_audit_tamper_and_legacy_snapshot_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="brain-audit-tamper-") as temp_dir:
            env = ControlledEnvironment(kind=EnvironmentKind.EVALUATION, root=temp_dir)
            env.authorize(ActionRequest(action="file_read", target="one.txt"))
            payload = env.snapshot()
            payload["audit"][0]["allowed"] = False
            with self.assertRaises(ValueError):
                ControlledEnvironment.from_snapshot(payload)
            legacy = env.snapshot()
            for key in ("root_fingerprint", "audit_head"):
                legacy.pop(key, None)
            legacy["audit"][0].pop("audit_hash", None)
            with self.assertRaises(ValueError):
                ControlledEnvironment.from_snapshot(legacy)

    def test_environment_snapshot_cannot_change_root_scope(self):
        with tempfile.TemporaryDirectory(prefix="brain-audit-root-") as temp_dir:
            with tempfile.TemporaryDirectory(prefix="brain-audit-other-") as other_dir:
                env = ControlledEnvironment(kind=EnvironmentKind.EVALUATION, root=temp_dir)
                payload = env.snapshot()
                payload["root"] = other_dir
                with self.assertRaises(ValueError):
                    ControlledEnvironment.from_snapshot(payload, expected_root=temp_dir)

    def test_bounded_audit_chain_roundtrips_after_prefix_eviction(self):
        with tempfile.TemporaryDirectory(prefix="brain-audit-bounded-") as temp_dir:
            env = ControlledEnvironment(
                kind=EnvironmentKind.EVALUATION, root=temp_dir, audit_limit=1
            )
            env.authorize(ActionRequest(action="file_read", target="one.txt"))
            env.authorize(ActionRequest(action="file_read", target="two.txt"))
            payload = env.snapshot()
            self.assertTrue(payload["audit_truncated"])
            restored = ControlledEnvironment.from_snapshot(payload, expected_root=temp_dir)
            self.assertEqual(len(restored.audit), 1)
            self.assertEqual(restored.audit[0].audit_hash, payload["audit_head"])

    def test_audit_limit_is_preserved_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="brain-audit-limit-") as temp_dir:
            env = ControlledEnvironment(
                kind=EnvironmentKind.EVALUATION, root=temp_dir, audit_limit=3
            )
            payload = env.snapshot()
            self.assertEqual(payload["audit_limit"], 3)
            restored = ControlledEnvironment.from_snapshot(payload, expected_root=temp_dir)
            self.assertEqual(restored.audit_limit, 3)
            tampered = dict(payload)
            tampered["audit_limit"] = 2
            with self.assertRaises(ValueError):
                ControlledEnvironment.from_snapshot(tampered, expected_root=temp_dir)

    def test_empty_resource_ledger_identity_is_preserved(self):
        budget = ResourceBudget(limits={"compute_ms": 100.0})
        ledger = ResourceLedger(max_events=7)
        controller = HomeostasisController(budget=budget, ledger=ledger, max_events=99)
        self.assertIs(controller.ledger, ledger)
        self.assertIs(controller.budget, budget)
        self.assertEqual(controller.ledger.max_events, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
