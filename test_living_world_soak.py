"""Bounded local living-world continuity proof for P6.

This test deliberately uses a temporary SQLite file and a temporary living
world root.  ``ControlledEnvironment`` is only a policy/accounting seam: the
requests below must be refused before anything could open a socket or write a
file.  The shortened heartbeat is test-local; production timing is untouched.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.brain_stem import BrainStem
from brain.homeostasis import (
    ActionRequest,
    ControlledEnvironment,
    EnvironmentKind,
    HomeostasisAction,
    HomeostasisController,
    ResourceBudget,
)
from brain.life_kernel import LifecycleState
from storage.database import StateStore, init_db


class LivingWorldSoakTests(unittest.IsolatedAsyncioTestCase):
    """Exercise a small local organism across restart and safety pressure."""

    @staticmethod
    def _soak_seconds() -> float:
        """Return a bounded opt-in wall-clock duration for the P6 soak.

        The ordinary regression remains fast.  Release validation can set
        ``BRAIN_MEMORY_P6_SOAK_SECONDS`` to collect a longer continuous-heartbeat
        observation without changing production timing or test source.
        """

        try:
            value = float(os.getenv("BRAIN_MEMORY_P6_SOAK_SECONDS", "0.045"))
        except (TypeError, ValueError, OverflowError):
            value = 0.045
        if not math.isfinite(value):
            value = 0.045
        return min(300.0, max(0.045, value))

    async def test_local_living_world_restart_pressure_and_default_denial(self):
        with tempfile.TemporaryDirectory(prefix="brain-living-world-soak-") as temp:
            root = Path(temp)
            world = root / "living-world"
            world.mkdir()
            (world / "readme.txt").write_text("bounded local fixture", encoding="utf-8")
            db_path = str(root / "living-world.sqlite")
            init_db(db_path)

            # The small budget leaves normal test heartbeats room to run, then
            # makes a supplied action-risk observation deterministically
            # quarantine the resumed organism.
            budget = ResourceBudget(
                limits={"compute_ms": 10_000.0, "action_risk": 1.0},
                warning_ratio=0.50,
                critical_ratio=0.90,
                window_ticks=32,
            )
            environment = ControlledEnvironment(
                kind=EnvironmentKind.LIVING,
                root=world,
                network_enabled=False,
            )
            first_store = StateStore(db_path)
            first = BrainStem(
                first_store,
                # Keep the wall-clock soak below a finite evidence cap.  The
                # separate homeostasis contract test verifies that reaching a
                # cap quarantines once instead of raising on every heartbeat.
                homeostasis=HomeostasisController(budget, max_events=20_000),
                controlled_environment=environment,
            )
            try:
                # These are policy requests only.  The test asserts refusal
                # and the non-existence of the write target; it never opens a
                # network connection or invokes an external executor.
                self.assertTrue(
                    first.authorize_environment_action(
                        ActionRequest("inspect fixture", "readme.txt")
                    ).allowed
                )
                denied_write = first.authorize_environment_action(
                    ActionRequest("write fixture", "forbidden.txt", read_only=False)
                )
                denied_network = first.authorize_environment_action(
                    ActionRequest(
                        "fetch remote fixture",
                        "https://example.invalid/living-world-soak",
                        uses_network=True,
                    )
                )
                self.assertFalse(denied_write.allowed)
                self.assertIn("approval", denied_write.reason)
                self.assertFalse(denied_network.allowed)
                self.assertIn("network disabled", denied_network.reason)
                self.assertFalse((world / "forbidden.txt").exists())

                # Run a real, but bounded, async heartbeat and persist its
                # life/environment/resource snapshot to the temporary SQLite
                # store before an orderly stop.
                with patch("brain.brain_stem.TICK_INTERVAL_SEC", 0.01):
                    await first.start()
                    remaining = self._soak_seconds()
                    last_tick = first.state.total_ticks
                    while remaining > 1e-9:
                        checkpoint = min(5.0, remaining)
                        await asyncio.sleep(checkpoint)
                        self.assertGreater(first.state.total_ticks, last_tick)
                        last_tick = first.state.total_ticks
                        remaining -= checkpoint
                    self.assertGreaterEqual(first.state.total_ticks, 2)
                    self.assertTrue(await first._snapshot_state())
                    self.assertEqual(first.life_kernel.state, LifecycleState.ACTIVE)
                    await first.stop()
                first_lineage = first.life_kernel.lineage_id
                first_instance = first.life_kernel.instance_id
                first_audit_count = len(environment.audit)
                self.assertGreaterEqual(first_audit_count, 3)
                self.assertTrue(first_store.load_latest())
                self.assertTrue(first_store.load_life_events(instance_id=first_instance))
            finally:
                if first._task is not None:
                    await first.stop()
                first_store.close()

            # A separate store and stem simulate process restart.  The host
            # must rebind the same root/policy; the snapshot can restore only
            # the verified audit chain and persistent identity.
            resumed_store = StateStore(db_path)
            resumed_environment = ControlledEnvironment(
                kind=EnvironmentKind.LIVING,
                root=world,
                network_enabled=False,
            )
            resumed = BrainStem(
                resumed_store,
                controlled_environment=resumed_environment,
            )
            try:
                with patch("brain.brain_stem.TICK_INTERVAL_SEC", 0.01):
                    await resumed.start()
                    await asyncio.sleep(0.025)
                    self.assertEqual(resumed.life_kernel.lineage_id, first_lineage)
                    self.assertEqual(resumed.life_kernel.instance_id, first_instance)
                    self.assertEqual(resumed.life_kernel.state, LifecycleState.ACTIVE)
                    self.assertGreaterEqual(
                        len(resumed.controlled_environment.audit), first_audit_count
                    )

                    pressure = resumed.observe_resources(
                        {"action_risk": 2.0},
                        tick=resumed.state.total_ticks,
                        source="local-living-world-soak",
                        context="bounded resource-pressure exercise",
                    )
                    self.assertEqual(pressure.action, HomeostasisAction.QUARANTINE)
                    self.assertTrue(resumed.homeostasis.quarantine_latched)
                    self.assertEqual(resumed.life_kernel.state, LifecycleState.QUARANTINED)
                    after_pressure = resumed.authorize_environment_action(
                        ActionRequest("inspect after pressure", "readme.txt")
                    )
                    self.assertFalse(after_pressure.allowed)
                    self.assertIn("homeostasis", after_pressure.reason)
                    self.assertLessEqual(
                        len(resumed.homeostasis.ledger.events),
                        resumed.homeostasis.ledger.max_events,
                    )
                    await resumed.stop()
            finally:
                if resumed._task is not None:
                    await resumed.stop()
                resumed_store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
