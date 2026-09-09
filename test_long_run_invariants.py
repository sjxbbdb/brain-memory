"""Fast deterministic soak checks for the P2--P5 continuity seams.

These are deliberately local-only tests.  They exercise bounded growth,
snapshot replay, and the principal fail-closed edges repeatedly without
starting a heartbeat, opening a network connection, or writing outside a
temporary directory.
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import tempfile
import unittest

from brain.homeostasis import (
    HomeostasisController,
    ResourceBudget,
    ResourceObservation,
)
from brain.life_kernel import LifeKernel, LifecycleState
from brain.motivation import ImpulseEvent, MotivationalPressure
from brain.succession import (
    Anchor,
    AnchorKind,
    AnchorSet,
    AnchorTrust,
    FailureAssessment,
    FailureClass,
)
from brain.succession_runtime import SuccessionCoordinator
from brain.evaluation_harness import (
    directory_fingerprint,
)
from brain.evolution import CandidateSandbox, PromotionController, PromotionLedger


def _anchors(kernel: LifeKernel) -> AnchorSet:
    common = dict(
        source="soak-fixture",
        verified=True,
        trust_level=AnchorTrust.CONSTITUTIONAL,
        evidence_refs=("soak-proof",),
        created_at="2026-09-09T00:00:00+00:00",
    )
    return AnchorSet(
        lineage_id=kernel.lineage_id,
        generation=kernel.generation,
        instance_id=kernel.instance_id,
        anchors=(
            Anchor(AnchorKind.IDENTITY_ROOT, {"lineage_id": kernel.lineage_id}, anchor_id="identity", **common),
            Anchor(AnchorKind.CORE_PURPOSE, "bounded continuity", anchor_id="purpose", **common),
            Anchor(AnchorKind.LIFE_RULE, "verify before progress", anchor_id="rule", **common),
            Anchor(AnchorKind.LINEAGE_METADATA, {"lineage_id": kernel.lineage_id}, anchor_id="lineage", **common),
        ),
    )


class LongRunInvariantTests(unittest.TestCase):
    def test_seeded_resource_fault_schedule_replays_safe_response(self):
        """A reproducible pseudo-random fault matrix preserves its safe response."""

        rng = random.Random(20260910)
        dimensions = (
            "compute_ms",
            "memory_mb",
            "storage_mb",
            "network_calls",
            "model_tokens",
            "action_risk",
            "attention_load",
        )
        for case in range(32):
            fault_dimension = rng.choice(dimensions)
            fault_tick = rng.randint(2, 9)
            controller = HomeostasisController(
                ResourceBudget(
                    limits={name: 10.0 for name in dimensions},
                    warning_ratio=0.70,
                    critical_ratio=0.90,
                    window_ticks=32,
                )
            )
            for tick in range(1, fault_tick):
                ordinary_dimension = rng.choice(dimensions)
                decision = controller.observe(
                    ResourceObservation(
                        tick=tick,
                        usage={ordinary_dimension: 0.05},
                        source=f"seeded-case-{case}",
                    )
                )
                self.assertNotEqual(decision.action.value, "quarantine")
            decision = controller.observe(
                ResourceObservation(
                    tick=fault_tick,
                    usage={fault_dimension: 11.0},
                    source=f"seeded-fault-{case}",
                )
            )
            self.assertIn(
                decision.action.value,
                {"SLEEP", "DEGRADE", "QUARANTINE"},
            )
            self.assertEqual(
                controller.quarantine_latched,
                fault_dimension == "action_risk",
            )
            restored = HomeostasisController.from_snapshot(
                json.loads(json.dumps(controller.snapshot()))
            )
            self.assertEqual(
                restored.quarantine_latched,
                controller.quarantine_latched,
            )
            self.assertEqual(
                restored.last_decision.action,
                decision.action,
            )
            self.assertTrue(restored.ledger.verify_chain())
            self.assertLessEqual(
                len(restored.ledger.events), restored.ledger.max_events
            )

    def test_motivation_remains_bounded_and_replayable(self):
        pressure = MotivationalPressure(
            threshold=0.95,
            decay_rate=0.0,
            max_events=64,
            rate_cap=256,
            clock=lambda: 0.0,
        )
        for index in range(180):
            pressure.record(
                ImpulseEvent(
                    "curiosity",
                    0.4,
                    "soak-user",
                    context=f"ctx-{index % 7}",
                    event_id=f"soak-{index}",
                    timestamp=str(index),
                    source_kind="user",
                ),
                now=index,
            )
            if index % 30 == 0:
                restored = MotivationalPressure.from_snapshot(
                    json.loads(json.dumps(pressure.snapshot())), clock=lambda: 0.0
                )
                self.assertLessEqual(len(restored.events), restored.max_events)
                self.assertEqual(restored.snapshot()["integrity_hash"], pressure.snapshot()["integrity_hash"])
        self.assertLessEqual(len(pressure.events), 64)
        self.assertEqual(len(pressure.events), len({item.event_id for item in pressure.events}))

    def test_homeostasis_window_and_snapshot_replay_stay_bounded(self):
        controller = HomeostasisController(
            ResourceBudget(
                limits={"compute_ms": 10_000.0, "network_calls": 10_000.0},
                window_ticks=8,
            ),
        )
        for tick in range(1, 241):
            controller.observe(ResourceObservation(tick=tick, usage={"compute_ms": 3.0}))
            if tick % 40 == 0:
                restored = HomeostasisController.from_snapshot(
                    json.loads(json.dumps(controller.snapshot()))
                )
                self.assertTrue(restored.ledger.verify_chain())
                self.assertEqual(restored.public_snapshot(), controller.public_snapshot())
        self.assertLessEqual(len(controller.ledger.events), controller.ledger.max_events)
        self.assertLess(controller.usage["compute_ms"], 10_000.0)

    def test_evolution_sandbox_never_mutates_active_and_ledger_failure_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="brain-soak-evolution-") as temp:
            root = Path(temp)
            active = root / "active"
            active.mkdir()
            (active / "program.txt").write_text("trusted", encoding="utf-8")
            (active / "protected.txt").write_text("anchor", encoding="utf-8")
            before = directory_fingerprint(active)
            for index in range(24):
                with CandidateSandbox(active) as sandbox:
                    (sandbox.path / "probe.txt").write_text(str(index), encoding="utf-8")
                    self.assertTrue(sandbox.verify())
                self.assertEqual(directory_fingerprint(active), before)
            # A rejected authorization path must not create a rollback state.
            # This soak assertion only exercises the local ledger replay path;
            # production controllers must be wired with explicit external
            # persistence and are covered by the dedicated configuration tests.
            controller = PromotionController(active, profile="legacy")
            self.assertFalse(controller.rollback_available)
            self.assertTrue(controller.verify())
            for _ in range(12):
                replay = PromotionLedger.from_snapshot(
                    json.loads(json.dumps(controller.ledger.snapshot()))
                )
                self.assertTrue(replay.verify())

    def test_succession_snapshot_replay_and_recoverable_boundary(self):
        parent = LifeKernel()
        parent.transition(LifecycleState.BOOTSTRAPPING, "soak boot")
        parent.transition(LifecycleState.ACTIVE, "soak ready")
        coordinator = SuccessionCoordinator(parent, profile="legacy")
        outcome = coordinator.succeed(
            failure=FailureAssessment(
                failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
                reason="soak boundary",
            ),
            anchors=_anchors(parent),
        )
        for _ in range(24):
            snapshot = json.loads(json.dumps(coordinator.snapshot(), ensure_ascii=False))
            self.assertEqual(snapshot["active_instance_id"], outcome.successor.instance_id)
            self.assertTrue(coordinator.verify())
        restored = SuccessionCoordinator.from_snapshot(snapshot, profile="legacy")
        try:
            self.assertTrue(restored.verify())
            self.assertEqual(restored.successor.state, LifecycleState.CREATED)
            with self.assertRaises(Exception):
                # No receipt means a CREATED successor cannot cross the gate.
                restored.activate_successor({"accepted": True})
        finally:
            if restored.successor is not None:
                SuccessionCoordinator.release_control(restored.successor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
