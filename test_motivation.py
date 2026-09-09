"""Unit tests for the bounded motivational-pressure boundary."""

import json
import threading
import unittest

from brain.motivation import (
    ChangeProposal,
    ImpulseEvent,
    IterationNeed,
    MotivationalPressure,
)


class MotivationTests(unittest.TestCase):
    def test_impulse_is_bounded_frozen_and_round_trips(self):
        event = ImpulseEvent.create(
            kind="CURIOUSITY",
            strength="9",
            source="sensor",
            context="a" * 500,
            timestamp="not-a-date",
            metadata={"nested": {"secret": "ignored as text"}, "n": float("inf")},
        )
        self.assertEqual(event.impulse_type, "curiousity")
        self.assertEqual(event.intensity, 1.0)
        self.assertLessEqual(len(event.context), 240)
        self.assertTrue(event.timestamp)
        with self.assertRaises(Exception):
            # Frozen records cannot be altered after observation.
            event.intensity = 0.1
        encoded = json.dumps(event.to_dict(), ensure_ascii=False)
        restored = ImpulseEvent.from_dict(json.loads(encoded))
        self.assertEqual(restored.event_id, event.event_id)
        self.assertEqual(restored.intensity, event.intensity)

    def test_frequency_intensity_persistence_and_context_accumulate(self):
        pressure = MotivationalPressure(
            threshold=0.60,
            decay_rate=0.0,
            frequency_window=100.0,
            event_gain_cap=0.5,
            clock=lambda: 0.0,
        )
        for index in range(4):
            pressure.record(
                ImpulseEvent(
                    "curiosity",
                    0.9,
                    "verified-observer",
                    context=f"context-{index}",
                    persistence=0.8,
                    event_id=f"event-{index}",
                    timestamp=str(index),
                    source_kind="verified",
                ),
                now=index,
            )
        self.assertGreaterEqual(pressure.pressure_for("curiosity"), 0.60)
        self.assertEqual(len(pressure.events), 4)
        need = pressure.poll_iteration_need("curiosity", now=4)
        self.assertIsInstance(need, IterationNeed)
        self.assertFalse(need.is_authorized)
        self.assertTrue(need.evidence)
        self.assertGreater(need.confidence, 0.0)

    def test_decay_and_hysteresis_do_not_oscillate(self):
        pressure = MotivationalPressure(
            threshold=0.10,
            release_threshold=0.05,
            decay_rate=0.1,
            cooldown_seconds=10,
            clock=lambda: 0.0,
        )
        pressure.record(ImpulseEvent("growth", 1.0, "user", event_id="one", timestamp="0", source_kind="user"), now=0)
        self.assertTrue(pressure.should_trigger("growth", now=0))
        self.assertIsNotNone(pressure.poll_iteration_need("growth", now=0))
        # Still above the release threshold: a second poll is suppressed.
        self.assertIsNone(pressure.poll_iteration_need("growth", now=1))
        pressure.update(now=100)
        self.assertLessEqual(pressure.pressure_for("growth"), 0.05)
        self.assertFalse(pressure.is_triggered("growth"))

    def test_cooldown_blocks_retrigger_after_release_until_expired(self):
        pressure = MotivationalPressure(
            threshold=0.15,
            release_threshold=0.14,
            decay_rate=0.0,
            cooldown_seconds=20,
            clock=lambda: 0.0,
        )
        pressure.record(ImpulseEvent("coherence", 1.0, "user", event_id="a", timestamp="0", source_kind="user"), now=0)
        self.assertIsNotNone(pressure.poll_iteration_need("coherence", now=0))
        # Resolve lowers pressure and clears hysteresis, but cooldown remains.
        pressure.resolve(event_id="a", now=1)
        self.assertIsNone(pressure.poll_iteration_need("coherence", now=2))
        pressure.record(ImpulseEvent("coherence", 1.0, "user", event_id="b", timestamp="21", source_kind="user"), now=21)
        self.assertIsNotNone(pressure.poll_iteration_need("coherence", now=21))

    def test_self_votes_are_rate_and_contribution_capped(self):
        pressure = MotivationalPressure(
            threshold=0.2,
            decay_rate=0.0,
            self_rate_cap=2,
            self_contribution_cap=0.1,
            clock=lambda: 0.0,
        )
        accepted = []
        for index in range(10):
            accepted.append(
                pressure.accept(
                    ImpulseEvent(
                        "growth",
                        1.0,
                        "self-model",
                        context="same",
                        source_kind="external",  # source text still forces self classification
                        self_generated=False,
                        event_id=f"self-{index}",
                        timestamp=str(index),
                    ),
                    now=index,
                )
            )
        self.assertLessEqual(sum(accepted), 2)
        self.assertGreaterEqual(pressure.total_self_rejected, 1)
        # Self reports alone cannot manufacture a threshold crossing here.
        self.assertLess(pressure.pressure_for("growth"), pressure.threshold)
        self.assertIsNone(pressure.poll_iteration_need("growth", now=20))

        # The invariant also holds if an embedding weakens numeric thresholds:
        # independent support is structurally required for emission.
        weak_policy = MotivationalPressure(
            threshold=0.01,
            decay_rate=0.0,
            self_rate_cap=3,
            self_contribution_cap=1.0,
            clock=lambda: 0.0,
        )
        for index in range(3):
            weak_policy.record(
                ImpulseEvent("growth", 1.0, "self-model", event_id=f"weak-{index}", timestamp=str(index)),
                now=index,
            )
        self.assertGreaterEqual(weak_policy.pressure_for("growth"), weak_policy.threshold)
        self.assertIsNone(weak_policy.poll_iteration_need("growth", now=3))

    def test_duplicate_and_rolling_rate_cap(self):
        pressure = MotivationalPressure(threshold=0.9, decay_rate=0.0, rate_cap=2, clock=lambda: 0.0)
        event = ImpulseEvent("curiosity", 1.0, "user", event_id="duplicate", timestamp="0", source_kind="user")
        self.assertTrue(pressure.accept(event, now=0))
        self.assertFalse(pressure.accept(event, now=0))
        self.assertTrue(pressure.accept(ImpulseEvent("curiosity", 1.0, "user", event_id="second", timestamp="1", source_kind="user"), now=1))
        self.assertFalse(pressure.accept(ImpulseEvent("curiosity", 1.0, "user", event_id="third", timestamp="2", source_kind="user"), now=2))
        self.assertEqual(len(pressure.events), 2)

    def test_concurrent_record_operations_are_atomic(self):
        """Workers cannot interleave deque/counter updates or corrupt a chain."""
        workers = 8
        per_worker = 200
        pressure = MotivationalPressure(
            threshold=0.95,
            decay_rate=0.0,
            rate_cap=workers * per_worker + 1,
            max_events=workers * per_worker + 1,
            clock=lambda: 0.0,
        )
        barrier = threading.Barrier(workers)
        errors = []

        def record_batch(worker_id):
            try:
                barrier.wait(timeout=5)
                for index in range(per_worker):
                    pressure.record(
                        ImpulseEvent(
                            "concurrency",
                            0.2,
                            "user",
                            event_id=f"worker-{worker_id}-{index}",
                            timestamp="0",
                            source_kind="user",
                        ),
                        now=0,
                    )
            except Exception as exc:  # assertion below reports the real failure
                errors.append(exc)

        threads = [threading.Thread(target=record_batch, args=(worker,)) for worker in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        expected = workers * per_worker
        self.assertEqual(pressure.total_received, expected)
        self.assertEqual(pressure.total_accepted, expected)
        self.assertEqual(pressure.total_rejected, 0)
        self.assertEqual(len(pressure.events), expected)
        self.assertEqual(len({event.event_id for event in pressure.events}), expected)

    def test_replay_guard_tracks_the_bounded_tail_without_dropping_live_ids(self):
        pressure = MotivationalPressure(
            threshold=0.9,
            decay_rate=0.0,
            max_events=8,
            rate_cap=100,
            clock=lambda: 0.0,
        )
        for index in range(20):
            pressure.accept(
                ImpulseEvent("x", 0.1, "user", event_id=f"tail-{index}", timestamp=str(index), source_kind="user"),
                now=index,
            )
        self.assertEqual(len(pressure.events), 8)
        # The newest retained ID remains deduplicated after the deque rolls.
        self.assertFalse(
            pressure.accept(
                ImpulseEvent("x", 0.1, "user", event_id="tail-19", timestamp="20", source_kind="user"),
                now=20,
            )
        )

    def test_snapshot_round_trip_preserves_pressure_without_authorization(self):
        pressure = MotivationalPressure(decay_rate=0.0, clock=lambda: 10.0)
        pressure.record(ImpulseEvent("connection", 0.8, "user", event_id="c1", timestamp="10", source_kind="user"), now=10)
        restored = MotivationalPressure.from_snapshot(json.loads(json.dumps(pressure.snapshot())), clock=lambda: 10.0)
        self.assertAlmostEqual(restored.pressure_for("connection"), pressure.pressure_for("connection"), places=5)
        self.assertEqual(restored.events[0].event_id, "c1")

    def test_snapshot_tamper_fails_closed(self):
        pressure = MotivationalPressure(decay_rate=0.0, clock=lambda: 0.0)
        pressure.record(ImpulseEvent("growth", 1.0, "user", event_id="g1", timestamp="0", source_kind="user"), now=0)
        snapshot = pressure.snapshot()
        snapshot["states"]["growth"]["pressure"] = 1.0
        restored = MotivationalPressure.from_snapshot(snapshot, clock=lambda: 0.0)
        self.assertTrue(restored.snapshot_rejected)
        self.assertEqual(restored.restore_error, "integrity_hash_mismatch")
        self.assertEqual(restored.get_pressure("growth"), 0.0)

    def test_explicit_replay_clock_works_without_custom_clock(self):
        pressure = MotivationalPressure(decay_rate=0.1)
        pressure.record(ImpulseEvent("curiosity", 1.0, "user", event_id="r1", timestamp="0", source_kind="user"), now=0)
        before = pressure.pressure_for("curiosity")
        pressure.update(now=100)
        self.assertLess(pressure.pressure_for("curiosity"), before)

    def test_change_proposal_is_record_only_and_protected_scope_rejected(self):
        need = IterationNeed(
            motive="growth",
            pressure=0.8,
            evidence=["verified gap"],
            source_labels=["observer"],
            confidence=0.8,
        )
        proposal = ChangeProposal.from_need(
            need,
            title="Improve parser",
            scope="brain/parser.py",
            hypothesis="fewer parse failures",
            expected_benefits=["reproducible accuracy gain"],
            risks=["regression"],
            resource_budget={"cpu_seconds": 30},
            baseline_revision="baseline-1",
            rollback_revision="baseline-1",
        )
        self.assertEqual(proposal.validate(), ())
        self.assertFalse(proposal.is_authorized)
        self.assertFalse(proposal.can_apply)
        self.assertNotIn("apply", dir(proposal))
        tampered = ChangeProposal.from_dict({**proposal.to_dict(), "authorized": True, "status": "approved", "scope": "life_kernel.py"})
        self.assertFalse(tampered.is_authorized)
        self.assertIn("protected_scope", tampered.validate())

    def test_incident_can_emit_need_but_not_authority(self):
        pressure = MotivationalPressure(threshold=0.9, decay_rate=0.0, clock=lambda: 0.0)
        need = pressure.record_incident("coherence", "integrity mismatch", severity=1.0, now=0)
        self.assertIsInstance(need, IterationNeed)
        self.assertEqual(need.trigger, "incident")
        self.assertFalse(need.is_authorized)


if __name__ == "__main__":
    unittest.main()
