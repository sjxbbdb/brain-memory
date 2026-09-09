"""Unit tests for the bounded motivational-pressure boundary."""

import json
import threading
import unittest

from brain.motivation import (
    ChangeProposal,
    ImpulseEvent,
    IterationNeed,
    MotivationSourceAttestor,
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

    def test_independent_sources_are_normalized_and_filtered(self):
        pressure = MotivationalPressure(
            decay_rate=0.0,
            rate_cap=32,
            clock=lambda: 0.0,
        )
        events = (
            # Same source identity despite case and surrounding whitespace.
            ImpulseEvent("growth", 1.0, " Sensor-A ", event_id="valid-a1", timestamp="0", source_kind="external"),
            ImpulseEvent("growth", 1.0, "sensor-a", event_id="valid-a2", timestamp="1", source_kind="user"),
            ImpulseEvent("growth", 1.0, "Sensor-B", event_id="valid-b", timestamp="2", source_kind="verified"),
            # These observations remain historical events but cannot provide
            # structural independent support.
            ImpulseEvent("growth", 0.0, "Sensor-C", event_id="zero-intensity", timestamp="3", source_kind="external"),
            ImpulseEvent("growth", 1.0, "Sensor-D", event_id="zero-weight", timestamp="4", source_kind="external", reliability=0.0),
            ImpulseEvent("growth", 1.0, "self-model", event_id="self-generated", timestamp="5", source_kind="external"),
            ImpulseEvent("growth", 1.0, "", event_id="empty-source", timestamp="6", source_kind="external"),
            ImpulseEvent("growth", 1.0, "resolved-source", event_id="resolved", timestamp="7", source_kind="external", unresolved=False),
        )
        for index, event in enumerate(events):
            self.assertTrue(pressure.accept(event, now=index))
        self.assertEqual(pressure.events[6].source, "")
        self.assertEqual(
            pressure.get_metrics("growth", now=8)["independent_source_count"],
            2,
        )
        self.assertEqual(
            pressure.get_metrics("growth", now=8)["independent_source_count"],
            len({"sensor-a", "sensor-b"}),
        )

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

    def test_bound_restore_cannot_be_downgraded_by_snapshot_config(self):
        """A host-bound accumulator keeps strict provenance across restore."""
        attestor = MotivationSourceAttestor(
            secret=b"motivation-restore-policy-secret", clock=lambda: 0.0
        )
        strict = MotivationalPressure(
            threshold=0.01,
            decay_rate=0.0,
            clock=lambda: 0.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        snapshot = strict.snapshot()
        snapshot["config"]["require_source_attestation"] = False
        # The integrity marker is intentionally not an authenticity boundary;
        # the test models an attacker who can rewrite the local snapshot.
        from brain.motivation import _integrity_digest

        snapshot["integrity_hash"] = _integrity_digest(
            {key: value for key, value in snapshot.items() if key != "integrity_hash"}
        )
        restored = MotivationalPressure.from_snapshot(
            snapshot, clock=lambda: 0.0, source_attestor=attestor
        )
        self.assertTrue(restored.require_source_attestation)
        forged = ImpulseEvent(
            "growth", 1.0, "verified", event_id="downgrade-forged", timestamp="0", source_kind="verified"
        )
        self.assertFalse(restored.accept(forged, now=0.0))
        self.assertIsNone(restored.poll_iteration_need("growth", now=0.0))

    def test_restore_drops_signed_provenance_and_redacts_legacy_event_fields(self):
        """Untrusted snapshots cannot expose or replay host signing material."""
        attestor = MotivationSourceAttestor(
            secret=b"motivation-restore-provenance-secret", clock=lambda: 0.0
        )
        base = ImpulseEvent(
            "growth",
            1.0,
            "private-source-label",
            context="private observation",
            event_id="signed-restore-1",
            timestamp="0",
            source_kind="external",
        )
        proof = attestor.issue_for_event(base, source_id="private-principal", now=0.0)
        signed_payload = base.to_dict()
        signed_payload["source"] = "RAW_LEGACY_SOURCE"
        signed_payload["source_label"] = "RAW_LEGACY_SOURCE"
        signed_payload["context"] = "RAW_LEGACY_CONTEXT"
        signed_payload["provenance"] = proof.to_dict(include_signature=True)
        snapshot = {
            "schema_version": 1,
            "config": {"decay_rate": 0.0, "require_source_attestation": True},
            "states": {"growth": {"pressure": 0.5}},
            "events": [{"event": signed_payload, "accepted_at": 0.0, "effective_weight": 0.5}],
        }
        from brain.motivation import _integrity_digest

        snapshot["integrity_hash"] = _integrity_digest(snapshot)
        restored = MotivationalPressure.from_snapshot(
            snapshot,
            clock=lambda: 0.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        self.assertEqual(len(restored.events), 1)
        restored_event = restored.events[0]
        self.assertIsNone(restored_event.provenance)
        encoded = json.dumps(restored_event.to_dict(), ensure_ascii=False)
        self.assertNotIn("RAW_LEGACY_SOURCE", encoded)
        self.assertNotIn("RAW_LEGACY_CONTEXT", encoded)
        self.assertNotIn(proof.signature, encoded)

    def test_forged_redaction_marker_labels_are_not_trusted(self):
        pressure = MotivationalPressure(decay_rate=0.0, clock=lambda: 0.0)
        event = ImpulseEvent(
            "growth",
            1.0,
            "<redacted:private-token:0123456789abcdef>",
            context="<redacted:secret@example.com:0123456789abcdef>",
            event_id="marker-1",
            timestamp="0",
            source_kind="external",
        )
        pressure.accept(event, now=0.0)
        encoded = json.dumps(pressure.snapshot(), ensure_ascii=False)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("secret@example.com", encoded)
        self.assertNotIn("<redacted:private-token", encoded)
        self.assertNotIn("<redacted:secret@example.com", encoded)

    def test_restore_normalizes_resolved_ids_with_redacted_event_ids(self):
        """Legacy URL-shaped IDs cannot bypass resolution bookkeeping."""
        raw_id = "https://evil.invalid/event?id=1"
        snapshot = {
            "schema_version": 1,
            "config": {"decay_rate": 0.0, "threshold": 0.01},
            "states": {"growth": {"pressure": 0.9}},
            "events": [
                {
                    "event": {
                        "impulse_type": "growth",
                        "intensity": 1.0,
                        "source": "external",
                        "source_kind": "user",
                        "event_id": raw_id,
                        "timestamp": "0",
                        "unresolved": True,
                    },
                    "accepted_at": 0.0,
                    "effective_weight": 0.8,
                }
            ],
            "resolved_ids": [raw_id],
            "seen_ids": [raw_id],
        }
        restored = MotivationalPressure.from_snapshot(snapshot, clock=lambda: 0.0)
        self.assertEqual(len(restored.events), 1)
        normalized_id = restored.events[0].event_id
        self.assertNotEqual(normalized_id, raw_id)
        self.assertFalse(restored.trigger_ready("growth", now=0.0))
        self.assertIsNone(restored.poll_iteration_need("growth", now=0.0))

    def test_redacted_category_round_trip_keeps_state_and_event_aligned(self):
        """Opaque motive/category markers remain stable across restarts."""
        pressure = MotivationalPressure(
            threshold=0.01, decay_rate=0.0, clock=lambda: 0.0
        )
        event = ImpulseEvent(
            "raw_user_payload_zz",
            1.0,
            "external-source",
            context="context-a",
            event_id="category-roundtrip-1",
            timestamp="0",
            source_kind="external",
        )
        self.assertTrue(pressure.accept(event, now=0.0))
        first = pressure.snapshot()
        restored = MotivationalPressure.from_snapshot(
            json.loads(json.dumps(first)), clock=lambda: 0.0
        )
        second = restored.snapshot()
        restored_again = MotivationalPressure.from_snapshot(
            json.loads(json.dumps(second)), clock=lambda: 0.0
        )
        self.assertEqual(restored.events[0].impulse_type, next(iter(restored.motives)))
        self.assertEqual(
            restored_again.events[0].impulse_type, next(iter(restored_again.motives))
        )
        self.assertEqual(
            restored.get_metrics(restored.events[0].impulse_type, now=0.0)["event_count"],
            1,
        )
        self.assertEqual(
            restored_again.get_metrics(restored_again.events[0].impulse_type, now=0.0)["event_count"],
            1,
        )
        self.assertEqual(second["states"].keys(), restored_again.snapshot()["states"].keys())

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

    def test_production_source_attestation_binds_event_and_rejects_replay(self):
        attestor = MotivationSourceAttestor(
            secret=b"motivation-source-test-secret", clock=lambda: 100.0
        )
        base = ImpulseEvent.create(
            impulse_type="growth",
            intensity=1.0,
            source="sensor-a",
            source_kind="external",
            event_id="attested-1",
            timestamp="100",
        )
        proof = attestor.issue_for_event(base, source_id="stable-sensor-a", now=100.0)
        event = ImpulseEvent.create(
            impulse_type="growth",
            intensity=1.0,
            source="sensor-a",
            source_kind="external",
            event_id="attested-1",
            timestamp="100",
            provenance=proof,
        )
        pressure = MotivationalPressure(
            threshold=0.01,
            decay_rate=0.0,
            clock=lambda: 100.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        self.assertTrue(pressure.accept(event, now=100.0))
        self.assertEqual(
            pressure.get_metrics("growth", now=100.0)["independent_source_count"],
            1,
        )
        # A second event cannot copy the consumed proof or alter the bound
        # source fields to manufacture another principal.
        forged = ImpulseEvent.create(
            impulse_type="growth",
            intensity=1.0,
            source="sensor-b",
            source_kind="verified",
            event_id="attested-2",
            timestamp="100",
            provenance=proof,
        )
        before = pressure.total_accepted
        pressure.record(forged, now=100.0)
        self.assertEqual(pressure.total_accepted, before)
        self.assertEqual(pressure.total_rejected, 1)

    def test_source_attestation_hash_binds_hidden_live_fields(self):
        base = ImpulseEvent(
            "growth",
            1.0,
            "private-source",
            context="private-context",
            event_id="hash-binding-1",
            timestamp="0",
            source_kind="external",
        )
        changed_source = ImpulseEvent(
            "growth",
            1.0,
            "different-source",
            context="private-context",
            event_id="hash-binding-1",
            timestamp="0",
            source_kind="external",
        )
        changed_context = ImpulseEvent(
            "growth",
            1.0,
            "private-source",
            context="different-context",
            event_id="hash-binding-1",
            timestamp="0",
            source_kind="external",
        )
        self.assertNotEqual(
            MotivationalPressure.event_payload_hash(base),
            MotivationalPressure.event_payload_hash(changed_source),
        )
        self.assertNotEqual(
            MotivationalPressure.event_payload_hash(base),
            MotivationalPressure.event_payload_hash(changed_context),
        )

    def test_source_proofs_are_consumed_only_after_admission_checks(self):
        attestor = MotivationSourceAttestor(
            secret=b"motivation-source-admission-secret", clock=lambda: 0.0
        )
        pressure = MotivationalPressure(
            decay_rate=0.0,
            rate_cap=1,
            clock=lambda: 0.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        first = ImpulseEvent.create(
            "growth", 1.0, "sensor-a", source_kind="external", event_id="admit-1", timestamp="0"
        )
        first = ImpulseEvent.create(
            "growth", 1.0, "sensor-a", source_kind="external", event_id="admit-1", timestamp="0",
            provenance=attestor.issue_for_event(first, source_id="sensor-a", now=0.0),
        )
        self.assertTrue(pressure.accept(first, now=0.0))
        second_base = ImpulseEvent.create(
            "growth", 1.0, "sensor-b", source_kind="external", event_id="admit-2", timestamp="0"
        )
        second = ImpulseEvent.create(
            "growth", 1.0, "sensor-b", source_kind="external", event_id="admit-2", timestamp="0",
            provenance=attestor.issue_for_event(second_base, source_id="sensor-b", now=0.0),
        )
        pressure.record(second, now=0.0)
        # The rate cap rejects before consuming the proof; a later accumulator
        # can still validate that same host-issued object.
        other = MotivationalPressure(
            decay_rate=0.0,
            clock=lambda: 0.0,
            source_attestor=attestor,
            require_source_attestation=True,
        )
        self.assertTrue(other.accept(second, now=0.0))

    def test_public_source_attestation_projection_is_opaque(self):
        attestor = MotivationSourceAttestor(
            secret=b"motivation-public-projection-secret", clock=lambda: 0.0
        )
        event = ImpulseEvent.create(
            "growth",
            1.0,
            "human-source-private-label",
            source_kind="external",
            event_id="attestation-public-1",
            timestamp="0",
        )
        proof = attestor.issue_for_event(
            event, source_id="private principal label", now=0.0
        )
        public = proof.public_dict()
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("private principal label", encoded)
        self.assertNotIn("motivation-public-projection-secret", encoded)
        self.assertNotIn(proof.signature, encoded)
        self.assertIn("attestation_hash", public)

    def test_snapshot_opaque_boundary_covers_identifier_and_category_payloads(self):
        pressure = MotivationalPressure(decay_rate=0.0, clock=lambda: 0.0)
        pressure.accept(
            ImpulseEvent.create(
                "raw_user_payload_zz",
                1.0,
                "source-label",
                context="private context",
                event_id="sk_abcdefghijklmnop",
                timestamp="0",
            ),
            now=0.0,
        )
        encoded = json.dumps(pressure.snapshot(), ensure_ascii=False)
        self.assertNotIn("raw_user_payload_zz", encoded)
        self.assertNotIn("sk_abcdefghijklmnop", encoded)
        self.assertNotIn("private context", encoded)

        need = IterationNeed(
            motive="growth",
            pressure=0.8,
            reason="RAW_USER_NEED_REASON",
            evidence=("RAW_USER_EVIDENCE",),
            source_labels=("RAW_USER_SOURCE",),
            need_id="RAW_USER_NEED_ID",
            created_at="not-a-timestamp",
        )
        need_encoded = json.dumps(need.to_dict(), ensure_ascii=False)
        self.assertNotIn("RAW_USER_NEED_REASON", need_encoded)
        self.assertNotIn("RAW_USER_EVIDENCE", need_encoded)
        self.assertNotIn("RAW_USER_SOURCE", need_encoded)
        self.assertNotIn("RAW_USER_NEED_ID", need_encoded)
        self.assertNotEqual(need.created_at, "not-a-timestamp")

    def test_public_motivation_snapshot_redacts_sensitive_observation_fields(self):
        pressure = MotivationalPressure(decay_rate=0.0, clock=lambda: 0.0)
        pressure.accept(
            ImpulseEvent.create(
                "growth",
                1.0,
                r"C:\Users\24763\Desktop\private-token.txt",
                context="ordinary-private-context https://example.invalid/?access_token=do-not-store",
                metadata={"api_key": "super-secret", "safe": "ordinary-private-value"},
                event_id="sensitive-1",
                timestamp="0",
            ),
            now=0.0,
        )
        snapshot = pressure.snapshot()
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("private-token.txt", encoded)
        self.assertNotIn("do-not-store", encoded)
        self.assertNotIn("super-secret", encoded)
        # Pattern matching is not a privacy boundary: ordinary user/tool
        # prose and source labels are opaque in the durable projection too.
        self.assertNotIn("ordinary-private-context", encoded)
        self.assertNotIn("ordinary-private-value", encoded)
        self.assertNotIn("safe", encoded)
        persisted_event = snapshot["events"][0]["event"]
        self.assertTrue(persisted_event["metadata"]["redacted"])
        self.assertTrue(persisted_event["source"].startswith("<redacted:source:"))
        self.assertTrue(persisted_event["context"].startswith("<redacted:context:"))
        self.assertIn("<redacted:", encoded)


if __name__ == "__main__":
    unittest.main()
