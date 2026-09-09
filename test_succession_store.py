"""SQLite persistence contracts for the append-only succession ledger."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from brain.succession import (
    Anchor,
    AnchorKind,
    AnchorSet,
    AnchorTrust,
    FailureAssessment,
    FailureClass,
    SuccessionRecord,
    filter_inheritance,
)
from brain.life_kernel import LifeKernel, LifecycleEvent, LifecycleState
from storage.database import (
    LIFE_CONTROL_LINEAGE_TABLE_SQL,
    LIFE_CONTROL_SCHEMA_META_TABLE_SQL,
    LIFE_LEDGER_TABLE_SQL,
    LifeControlLease,
    StateStore,
    _contains_secret_field,
    init_db,
)


def _record(*, lineage: str = "lineage-store", parent_generation: int = 0,
            parent_instance: str = "parent-0", successor_instance: str = "child-1",
            previous_hash: str = "") -> SuccessionRecord:
    common = dict(
        source="trusted-observer",
        verified=True,
        trust_level=AnchorTrust.CONSTITUTIONAL,
        evidence_refs=("proof-1",),
        created_at="2026-09-09T00:00:00+00:00",
    )
    anchors = AnchorSet(
        lineage_id=lineage,
        generation=parent_generation,
        instance_id=parent_instance,
        anchors=(
            Anchor(AnchorKind.IDENTITY_ROOT, {"lineage_id": lineage}, anchor_id="identity", **common),
            Anchor(AnchorKind.CORE_PURPOSE, "purpose", anchor_id="purpose", **common),
            Anchor(AnchorKind.LIFE_RULE, "audit", anchor_id="rule", **common),
            Anchor(AnchorKind.LINEAGE_METADATA, {"lineage_id": lineage}, anchor_id="lineage", **common),
        ),
    )
    plan = filter_inheritance(anchors, successor_generation=parent_generation + 1)
    failure = FailureAssessment(
        failure_class=FailureClass.HARD_INTEGRITY_FAILURE,
        reason="ledger mismatch",
        evidence_refs=("proof-1",),
        confirmed=True,
        incident_id=f"incident-{parent_generation}",
        detected_at="2026-09-09T00:01:00+00:00",
    )
    return SuccessionRecord.create(
        lineage_id=lineage,
        parent_instance_id=parent_instance,
        parent_generation=parent_generation,
        successor_instance_id=successor_instance,
        failure=failure,
        inheritance=plan,
        parent_frozen_at="2026-09-09T00:02:00+00:00",
        created_at="2026-09-09T00:03:00+00:00",
        previous_hash=previous_hash,
    )


class SecretFieldGuardTests(unittest.TestCase):
    def test_compound_sensitive_key_spellings_are_rejected(self):
        # Values are placeholders only; the guard must reject by field name
        # without reading or logging any credential material.
        sensitive_keys = (
            "secret_key",
            "private_key",
            "signing_key",
            "client_secret",
            "api_secret",
            "clientSecret",
            "authorization",
            "authorization-header",
            "jwt",
            "bearer",
            "oauth_token",
        )
        for key in sensitive_keys:
            with self.subTest(key=key):
                self.assertTrue(
                    _contains_secret_field({"nested": [{key: "[REDACTED_TEST_VALUE]"}]})
                )

        # Similar-looking ordinary fields remain usable; detection is based
        # on key components rather than a substring in arbitrary text.
        self.assertFalse(_contains_secret_field({"secretary": "ordinary"}))
        self.assertFalse(_contains_secret_field({"key_id": "ordinary"}))
        self.assertFalse(
            _contains_secret_field({"description": "authorization is required"})
        )


class LifeControlLeaseTests(unittest.TestCase):
    def test_claim_is_unique_and_release_requires_exact_proof(self):
        with tempfile.TemporaryDirectory(prefix="life-control-lease-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            # Idempotent schema initialization must not alter the lease table.
            init_db(path)
            first_store = StateStore(path)
            second_store = StateStore(path)
            first = first_store.claim_life_control(
                "lineage-lease",
                "instance-0",
                "owner-a",
                generation=0,
                ttl_sec=30,
                now=100.0,
            )
            self.assertIsInstance(first, LifeControlLease)
            assert first is not None
            self.assertTrue(first_store.assert_life_control(first, now=101.0))
            self.assertIsNone(
                second_store.claim_life_control(
                    "lineage-lease",
                    "instance-1",
                    "owner-b",
                    generation=1,
                    ttl_sec=30,
                    now=101.0,
                )
            )
            self.assertFalse(
                second_store.release_life_control(
                    first.lineage_id,
                    first.instance_id,
                    "owner-b",
                    first.lease_token,
                    first.fencing,
                )
            )
            self.assertFalse(
                first_store.release_life_control(
                    first.lineage_id,
                    first.instance_id,
                    first.owner_id,
                    "wrong-token",
                    first.fencing,
                )
            )
            self.assertTrue(first_store.release_life_control(first, now=102.0))
            self.assertIsNone(first_store.get_life_control(first.lineage_id))
            first_store.close()
            second_store.close()

    def test_expired_takeover_fences_old_owner(self):
        with tempfile.TemporaryDirectory(prefix="life-control-expiry-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            old_store = StateStore(path)
            new_store = StateStore(path)
            old = old_store.claim_life_control(
                "lineage-expiry",
                "instance-0",
                "owner-a",
                ttl_sec=5,
                now=200.0,
            )
            self.assertIsNotNone(old)
            assert old is not None
            # An expired owner cannot delete the tombstone and reset fencing.
            self.assertFalse(old_store.release_life_control(old, now=205.0))
            fresh = new_store.claim_life_control(
                "lineage-expiry",
                # Before a genesis exists, takeover may change the owner but
                # must keep the reserved identity/generation.  A child
                # identity is authorized only by the handover seam below.
                "instance-0",
                "owner-b",
                generation=0,
                ttl_sec=5,
                now=205.0,
            )
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertEqual(fresh.fencing, old.fencing + 1)
            self.assertFalse(old_store.assert_life_control(old, now=205.1))
            self.assertIsNone(old_store.renew_life_control(old, now=205.1))
            self.assertFalse(old_store.release_life_control(old))
            self.assertTrue(new_store.assert_life_control(fresh, now=206.0))
            self.assertTrue(new_store.release_life_control(fresh, now=206.0))
            old_store.close()
            new_store.close()

    def test_pre_genesis_expired_takeover_cannot_change_identity(self):
        with tempfile.TemporaryDirectory(prefix="life-control-pre-genesis-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            lease = store.claim_life_control(
                "lineage-pre-genesis",
                "instance-original",
                "owner-original",
                generation=0,
                ttl_sec=5,
                now=100.0,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertIsNone(
                store.claim_life_control(
                    "lineage-pre-genesis",
                    "instance-forged",
                    "owner-new",
                    generation=7,
                    ttl_sec=30,
                    now=106.0,
                )
            )
            store.close()

    def test_event_head_is_compare_and_set_and_append_is_atomic(self):
        with tempfile.TemporaryDirectory(prefix="life-control-head-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            genesis = LifecycleEvent.create(
                sequence=1,
                lineage_id="lineage-head",
                generation=0,
                instance_id="instance-head",
                event_type="created",
                from_state=None,
                to_state=LifecycleState.CREATED,
                reason="genesis",
            )
            self.assertTrue(store.append_life_event(genesis.to_dict()))
            lease = store.claim_life_control(
                "lineage-head",
                "instance-head",
                "owner-head",
                generation=0,
                ttl_sec=30,
                now=100.0,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.last_event_hash, genesis.event_hash)
            self.assertIsNone(
                store.claim_life_control(
                    "lineage-head",
                    "instance-head",
                    "owner-head",
                    generation=1,
                    ttl_sec=30,
                    now=101.0,
                    lease_token=lease.lease_token,
                    expected_last_event_hash=genesis.event_hash,
                )
            )
            self.assertIsNone(
                store.claim_life_control(
                    "lineage-head",
                    "instance-head",
                    "owner-head",
                    generation=0,
                    ttl_sec=30,
                    now=101.0,
                    lease_token=lease.lease_token,
                    expected_last_event_hash="0" * 64,
                )
            )
            transition = LifecycleEvent.create(
                sequence=2,
                lineage_id="lineage-head",
                generation=0,
                instance_id="instance-head",
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.BOOTSTRAPPING,
                reason="boot",
                previous_hash=genesis.event_hash,
            )
            self.assertTrue(
                store.append_life_event_with_lease(transition.to_dict(), lease, now=101.0)
            )
            persisted = store.get_life_control("lineage-head")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.last_event_hash, transition.event_hash)

            # The old in-memory lease head is fenced from appending a second
            # event after the durable head has advanced.
            next_event = LifecycleEvent.create(
                sequence=3,
                lineage_id="lineage-head",
                generation=0,
                instance_id="instance-head",
                event_type="lifecycle_transition",
                from_state=LifecycleState.BOOTSTRAPPING,
                to_state=LifecycleState.ACTIVE,
                reason="ready",
                previous_hash=transition.event_hash,
            )
            self.assertFalse(
                store.append_life_event_with_lease(next_event.to_dict(), lease, now=102.0)
            )
            self.assertEqual(len(store.load_life_events(instance_id="instance-head")), 2)

            refreshed = store.renew_life_control(lease, now=102.0)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.last_event_hash, transition.event_hash)
            self.assertTrue(
                store.append_life_event_with_lease(next_event.to_dict(), refreshed, now=103.0)
            )
            self.assertEqual(len(store.load_life_events(instance_id="instance-head")), 3)
            store.close()

    def test_control_marker_survives_release_and_hides_lease_capability(self):
        with tempfile.TemporaryDirectory(prefix="life-control-marker-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            genesis = LifecycleEvent.create(
                sequence=1,
                lineage_id="lineage-marker",
                generation=0,
                instance_id="instance-marker",
                event_type="created",
                from_state=None,
                to_state=LifecycleState.CREATED,
                reason="genesis",
            )
            self.assertTrue(store.append_life_event(genesis.to_dict()))
            lease = store.claim_life_control(
                genesis.lineage_id,
                genesis.instance_id,
                "owner-marker",
                generation=0,
                ttl_sec=30,
                now=100,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            live_view = store.get_life_control(genesis.lineage_id)
            self.assertIsNotNone(live_view)
            assert live_view is not None
            self.assertIsNone(live_view.lease_token)
            self.assertIsNone(store.renew_life_control(live_view, now=100.5))
            self.assertTrue(store.release_life_control(lease, now=101))
            view = store.get_life_control(genesis.lineage_id)
            self.assertIsNone(view)
            transition = LifecycleEvent.create(
                sequence=2,
                lineage_id=genesis.lineage_id,
                generation=0,
                instance_id=genesis.instance_id,
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.BOOTSTRAPPING,
                reason="must-reclaim",
                previous_hash=genesis.event_hash,
            )
            self.assertFalse(store.append_life_event(transition.to_dict(), now=102))
            fresh = store.claim_life_control(
                genesis.lineage_id,
                genesis.instance_id,
                "owner-reclaimed",
                generation=0,
                ttl_sec=30,
                now=102,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertGreater(fresh.fencing, lease.fencing)
            self.assertTrue(store.append_life_event_with_lease(transition.to_dict(), fresh, now=103))
            store.close()

    def test_lease_operations_sample_real_clock_after_transaction_lock(self):
        class DelayedStore(StateStore):
            def _life_control_connection(self):  # type: ignore[override]
                time.sleep(0.20)
                return super()._life_control_connection()

        with tempfile.TemporaryDirectory(prefix="life-control-clock-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            base = StateStore(path)
            lease = base.claim_life_control(
                "lineage-clock",
                "instance-clock",
                "owner-clock",
                ttl_sec=0.05,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            delayed = DelayedStore(path)
            # The connection delay exceeds the TTL.  With a real clock sampled
            # only after BEGIN IMMEDIATE, renewal must fail closed.
            self.assertIsNone(delayed.renew_life_control(lease, ttl_sec=30))
            self.assertFalse(delayed.assert_life_control(lease))
            delayed.close()
            base.close()

    def test_lease_schema_exposes_event_head_without_owner_unique_constraint(self):
        with tempfile.TemporaryDirectory(prefix="life-control-schema-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            conn = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(life_control_lease)")
                }
                self.assertIn("last_event_hash", columns)
                schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='life_control_lease'"
                ).fetchone()[0].lower()
                self.assertNotIn("unique(lineage_id, owner_id)", schema.replace(" ", ""))
            finally:
                conn.close()

    def test_expired_token_cannot_append_after_takeover(self):
        with tempfile.TemporaryDirectory(prefix="life-control-fence-append-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            genesis = LifecycleEvent.create(
                sequence=1,
                lineage_id="lineage-fence-append",
                generation=0,
                instance_id="instance-fence-append",
                event_type="created",
                from_state=None,
                to_state=LifecycleState.CREATED,
                reason="genesis",
            )
            self.assertTrue(store.append_life_event(genesis.to_dict()))
            old = store.claim_life_control(
                "lineage-fence-append",
                "instance-fence-append",
                "owner-old",
                generation=0,
                ttl_sec=5,
                now=100.0,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(old)
            assert old is not None
            fresh = store.claim_life_control(
                "lineage-fence-append",
                "instance-fence-append",
                "owner-new",
                generation=0,
                ttl_sec=30,
                now=105.0,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertGreater(fresh.fencing, old.fencing)
            event = LifecycleEvent.create(
                sequence=2,
                lineage_id="lineage-fence-append",
                generation=0,
                instance_id="instance-fence-append",
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.BOOTSTRAPPING,
                reason="boot",
                previous_hash=genesis.event_hash,
            )
            self.assertFalse(
                store.append_life_event_with_lease(event.to_dict(), old, now=106.0)
            )
            forged = LifeControlLease(
                lineage_id=old.lineage_id,
                instance_id=old.instance_id,
                generation=old.generation,
                owner_id=old.owner_id,
                lease_token="wrong-token",
                fencing=old.fencing,
                acquired_at=old.acquired_at,
                renewed_at=old.renewed_at,
                expires_at=old.expires_at,
                last_event_hash=old.last_event_hash,
            )
            self.assertFalse(
                store.append_life_event_with_lease(event.to_dict(), forged, now=106.0)
            )
            self.assertTrue(
                store.append_life_event_with_lease(event.to_dict(), fresh, now=106.0)
            )
            self.assertEqual(len(store.load_life_events(instance_id="instance-fence-append")), 2)
            store.close()

    def test_legacy_lease_schema_migrates_without_losing_head(self):
        with tempfile.TemporaryDirectory(prefix="life-control-migrate-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """CREATE TABLE life_control_lease (
                        lineage_id TEXT PRIMARY KEY,
                        instance_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        owner_id TEXT NOT NULL,
                        lease_token TEXT NOT NULL UNIQUE,
                        fencing INTEGER NOT NULL,
                        acquired_at REAL NOT NULL,
                        renewed_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        UNIQUE(lineage_id, instance_id),
                        UNIQUE(lineage_id, owner_id)
                    )"""
                )
                conn.execute(
                    "INSERT INTO life_control_lease "
                    "(lineage_id, instance_id, generation, owner_id, lease_token, fencing, "
                    "acquired_at, renewed_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("lineage-migrate", "instance-migrate", 0, "owner-migrate", "token-migrate", 3, 1, 1, 100),
                )
                conn.commit()
            finally:
                conn.close()
            init_db(path)
            store = StateStore(path)
            migrated = store.get_life_control("lineage-migrate")
            self.assertIsNotNone(migrated)
            assert migrated is not None
            self.assertEqual(migrated.last_event_hash, "")
            # The old owner uniqueness is gone after the rebuild; the same
            # owner can still be represented on a different lineage.
            other = store.claim_life_control(
                "lineage-other",
                "instance-other",
                "owner-migrate",
                generation=0,
                now=10.0,
                ttl_sec=30,
            )
            self.assertIsNotNone(other)
            verify_conn = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in verify_conn.execute("PRAGMA table_info(life_control_lease)")
                }
                self.assertIn("last_event_hash", columns)
            finally:
                verify_conn.close()
            store.close()

    def test_unleased_new_event_is_rejected_while_lease_row_exists(self):
        """A raw StateStore handle cannot inject history around a lease."""

        with tempfile.TemporaryDirectory(prefix="life-control-unleased-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            genesis = LifecycleEvent.create(
                sequence=1,
                lineage_id="lineage-unleased",
                generation=0,
                instance_id="instance-unleased",
                event_type="created",
                from_state=None,
                to_state=LifecycleState.CREATED,
                reason="genesis",
            )
            self.assertTrue(store.append_life_event(genesis.to_dict()))
            lease = store.claim_life_control(
                "lineage-unleased",
                "instance-unleased",
                "owner-unleased",
                generation=0,
                ttl_sec=5,
                now=100,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            transition = LifecycleEvent.create(
                sequence=2,
                lineage_id=genesis.lineage_id,
                generation=genesis.generation,
                instance_id=genesis.instance_id,
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.BOOTSTRAPPING,
                reason="boot",
                previous_hash=genesis.event_hash,
            )
            # The row is still present even after expiry; an explicit takeover
            # must fence the old owner before a fresh event can be written.
            self.assertFalse(store.append_life_event(transition.to_dict(), now=106))
            fresh = store.claim_life_control(
                "lineage-unleased",
                "instance-unleased",
                "owner-new-unleased",
                generation=0,
                ttl_sec=30,
                now=106,
                expected_last_event_hash=genesis.event_hash,
            )
            self.assertIsNotNone(fresh)
            assert fresh is not None
            self.assertTrue(
                store.append_life_event_with_lease(transition.to_dict(), fresh, now=107)
            )
            self.assertEqual(len(store.load_life_events(instance_id=genesis.instance_id)), 2)
            store.close()

    def test_intermediate_schema_with_meta_but_empty_markers_fails_closed(self):
        """A crashed upgrade cannot reopen an old unleased lineage."""

        with tempfile.TemporaryDirectory(prefix="life-control-partial-migration-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            genesis = LifecycleEvent.create(
                sequence=1,
                lineage_id="lineage-partial-migration",
                generation=0,
                instance_id="instance-partial-migration",
                event_type="created",
                from_state=None,
                to_state=LifecycleState.CREATED,
                reason="legacy-genesis",
            )
            conn = sqlite3.connect(path)
            try:
                # Simulate the intermediate checkout: the immutable ledger and
                # schema meta row were committed, but marker insertion did not
                # happen before the process stopped.
                conn.execute(LIFE_LEDGER_TABLE_SQL)
                conn.execute(LIFE_CONTROL_LINEAGE_TABLE_SQL)
                conn.execute(LIFE_CONTROL_SCHEMA_META_TABLE_SQL)
                conn.execute(
                    "INSERT INTO life_control_schema_meta (id, initialized_at) VALUES (1, 1.0)"
                )
                payload = genesis.to_dict()
                conn.execute(
                    "INSERT INTO life_ledger "
                    "(event_id, lineage_id, generation, instance_id, sequence, timestamp, "
                    "event_type, from_state, to_state, reason, metadata, previous_hash, event_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        payload["event_id"],
                        payload["lineage_id"],
                        payload["generation"],
                        payload["instance_id"],
                        payload["sequence"],
                        payload["timestamp"],
                        payload["event_type"],
                        payload["from_state"],
                        payload["to_state"],
                        payload["reason"],
                        json.dumps(payload["metadata"], ensure_ascii=False, sort_keys=True),
                        payload["previous_hash"],
                        payload["event_hash"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            init_db(path)
            verify_conn = sqlite3.connect(path)
            try:
                marker = verify_conn.execute(
                    "SELECT reason FROM life_control_lineage WHERE lineage_id = ?",
                    (genesis.lineage_id,),
                ).fetchone()
                self.assertEqual(marker[0], "legacy_ledger_migrated")
            finally:
                verify_conn.close()
            store = StateStore(path)
            transition = LifecycleEvent.create(
                sequence=2,
                lineage_id=genesis.lineage_id,
                generation=0,
                instance_id=genesis.instance_id,
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.BOOTSTRAPPING,
                reason="must-claim",
                previous_hash=genesis.event_hash,
            )
            self.assertFalse(store.append_life_event(transition.to_dict()))
            store.close()

    def test_handover_genesis_advances_fenced_head_for_crash_recovery(self):
        """Staged child genesis is durable, but cannot claim before sealing."""

        with tempfile.TemporaryDirectory(prefix="life-control-handover-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            parent = LifeKernel()
            parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
            parent.transition(LifecycleState.ACTIVE, "ready")
            child = LifeKernel(
                identity_core=parent.identity_core,
                generation=parent.generation + 1,
                parent_instance_id=parent.instance_id,
            )
            parent.transition(LifecycleState.QUARANTINED, "fault")
            parent.transition(
                LifecycleState.SUCCESSION_PENDING,
                "handover",
                metadata={"successor_instance_id": child.instance_id},
                event_type="succession_pending",
            )
            self.assertTrue(store.append_life_event(parent.ledger.events[0].to_dict()))
            lease = store.claim_life_control(
                parent.lineage_id,
                parent.instance_id,
                "owner-handover",
                generation=parent.generation,
                ttl_sec=5,
                now=200,
                expected_last_event_hash=parent.ledger.events[0].event_hash,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            for index, event in enumerate(parent.ledger.events[1:], start=1):
                self.assertTrue(
                    store.append_life_event_with_lease(
                        event.to_dict(), lease, now=200 + index
                    )
                )
                refreshed_parent = store.renew_life_control(
                    lease, now=200 + index + 0.1
                )
                self.assertIsNotNone(refreshed_parent)
                assert refreshed_parent is not None
                lease = refreshed_parent
            child_genesis = child.ledger.events[0]
            self.assertTrue(
                store.append_life_event_handover(
                    child_genesis.to_dict(),
                    lease,
                    successor_instance_id=child.instance_id,
                    now=201,
                )
            )
            persisted = store.get_life_control(parent.lineage_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.last_event_hash, child_genesis.event_hash)
            # A child head is fenced durably, but the remaining anchor/record/
            # seal proof is still required before a new host may claim it.
            successor_lease = store.claim_life_control(
                parent.lineage_id,
                child.instance_id,
                "owner-successor",
                generation=child.generation,
                ttl_sec=30,
                # The final parent heartbeat above renews with the default
                # 60-second TTL.  Advance beyond that lease expiry before
                # simulating takeover; claiming while it is still live must
                # remain rejected.
                now=266,
                expected_last_event_hash=child_genesis.event_hash,
            )
            self.assertIsNone(successor_lease)
            self.assertFalse(store.assert_life_control(lease, now=266.1))
            store.close()

    def test_handover_rejects_terminal_parent_direct_write(self):
        """Pre-staging terminal rows remain a read/migration boundary only."""

        with tempfile.TemporaryDirectory(prefix="life-control-terminal-write-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            parent = LifeKernel()
            parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
            parent.transition(LifecycleState.ACTIVE, "ready")
            parent.transition(LifecycleState.QUARANTINED, "fault")
            parent.transition(LifecycleState.SUCCESSION_PENDING, "handover")
            parent.transition(LifecycleState.DEAD, "sealed")
            for event in parent.ledger.events:
                self.assertTrue(store.append_life_event(event.to_dict()))
            lease = store.claim_life_control(
                parent.lineage_id,
                parent.instance_id,
                "owner-terminal-write",
                generation=parent.generation,
                ttl_sec=5,
                now=200,
                expected_last_event_hash=parent.ledger.events[-1].event_hash,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            child = LifeKernel(
                identity_core=parent.identity_core,
                generation=parent.generation + 1,
                parent_instance_id=parent.instance_id,
            )
            self.assertFalse(
                store.append_life_event_handover(
                    child.ledger.events[0].to_dict(),
                    lease,
                    successor_instance_id=child.instance_id,
                    now=201,
                )
            )
            self.assertEqual(store.load_life_events(instance_id=child.instance_id), [])
            self.assertTrue(store.release_life_control(lease, now=202))
            store.close()


class SuccessionStoreTests(unittest.TestCase):
    def test_record_with_existing_parent_chain_requires_terminal_head(self):
        """A lifecycle-backed succession row cannot bypass parent sealing."""

        with tempfile.TemporaryDirectory(prefix="succession-parent-boundary-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)

            active_parent = LifeKernel()
            active_parent.transition(LifecycleState.BOOTSTRAPPING, "boot")
            active_parent.transition(LifecycleState.ACTIVE, "ready")
            for event in active_parent.ledger.events:
                self.assertTrue(store.append_life_event(event.to_dict()))

            active_record = _record(
                lineage=active_parent.lineage_id,
                parent_generation=active_parent.generation,
                parent_instance=active_parent.instance_id,
            )
            self.assertFalse(store.append_succession_record(active_record))
            self.assertEqual(store.load_succession_records(), [])

            terminal_parent = LifeKernel()
            terminal_parent.transition(LifecycleState.RETIRED, "sealed")
            for event in terminal_parent.ledger.events:
                self.assertTrue(store.append_life_event(event.to_dict()))
            terminal_record = _record(
                lineage=terminal_parent.lineage_id,
                parent_generation=terminal_parent.generation,
                parent_instance=terminal_parent.instance_id,
            )
            self.assertTrue(store.append_succession_record(terminal_record))
            self.assertEqual(
                len(store.load_succession_records(lineage_id=terminal_parent.lineage_id)),
                1,
            )
            store.close()

    def test_append_load_reopen_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="succession-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            first = _record()
            self.assertTrue(store.append_succession_record(first))
            self.assertTrue(store.verify_succession_ledger())
            loaded = store.load_succession_records(lineage_id=first.lineage_id)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["record_hash"], first.record_hash)
            self.assertEqual(loaded[0]["successor_generation"], 1)
            store.close()

            reopened = StateStore(path)
            self.assertTrue(reopened.verify_succession_ledger(lineage_id=first.lineage_id))
            typed = reopened.load_succession_record_objects(lineage_id=first.lineage_id)
            self.assertEqual(typed[0].to_dict(), first.to_dict())
            reopened.close()

    def test_duplicate_is_idempotent_but_conflicts_and_stale_chain_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="succession-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            first = _record()
            self.assertTrue(store.append_succession_record(first))
            self.assertTrue(store.append_succession_record(first))

            # Same record_id with a different immutable payload is a conflict.
            conflict_payload = first.to_dict()
            conflict_payload["reason"] = "rewritten"
            self.assertFalse(store.append_succession_record(conflict_payload))

            # A second generation must point at the first record hash.
            stale = _record(
                parent_generation=1,
                parent_instance="child-1",
                successor_instance="child-2",
                previous_hash="stale-hash",
            )
            self.assertFalse(store.append_succession_record(stale))
            second = _record(
                parent_generation=1,
                parent_instance="child-1",
                successor_instance="child-2",
                previous_hash=first.record_hash,
            )
            self.assertTrue(store.append_succession_record(second))
            self.assertTrue(store.verify_succession_ledger())
            self.assertEqual(len(store.load_succession_records()), 2)
            store.close()

    def test_schema_and_api_are_insert_only_and_do_not_store_secret_payloads(self):
        with tempfile.TemporaryDirectory(prefix="succession-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            # A credential anchor is excluded by the inheritance filter; its
            # value must not occur in the persisted projection.
            common = dict(
                source="trusted-observer",
                verified=True,
                trust_level=AnchorTrust.CONSTITUTIONAL,
                evidence_refs=("proof",),
            )
            base = _record()
            # Use the already valid record's plan and append a separate record
            # whose excluded decision contains no credential value.
            self.assertTrue(store.append_succession_record(base))
            raw = json.dumps(store.load_succession_records()[0], ensure_ascii=False)
            self.assertNotIn("API-KEY-SECRET", raw)

            conn = sqlite3.connect(path)
            try:
                columns = [row[1].lower() for row in conn.execute("PRAGMA table_info(succession_ledger)")]
                self.assertFalse(any(token in column for column in columns for token in ("credential", "token", "password", "secret", "cookie")))
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("UPDATE succession_ledger SET reason='rewrite'")
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("DELETE FROM succession_ledger")
            finally:
                conn.close()
            self.assertTrue(store.verify_succession_ledger())
            store.close()

    def test_tampering_index_or_payload_fails_verification(self):
        with tempfile.TemporaryDirectory(prefix="succession-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            first = _record()
            self.assertTrue(store.append_succession_record(first))
            # Triggers protect normal SQL mutation; use a copied database row
            # through an explicit trigger drop solely to exercise verification.
            conn = sqlite3.connect(path)
            try:
                conn.execute("DROP TRIGGER succession_ledger_no_update")
                conn.execute("UPDATE succession_ledger SET storage_hash='tampered'")
                conn.commit()
            finally:
                conn.close()
            self.assertFalse(store.verify_succession_ledger())
            store.close()


class AnchorVaultStoreTests(unittest.TestCase):
    def _set(self, *, lineage: str = "lineage-vault", generation: int = 0,
             instance: str = "instance-vault") -> AnchorSet:
        common = dict(
            source="trusted-observer",
            verified=True,
            trust_level=AnchorTrust.CONSTITUTIONAL,
            evidence_refs=("vault-proof",),
            created_at="2026-09-09T00:10:00+00:00",
        )
        return AnchorSet(
            lineage_id=lineage,
            generation=generation,
            instance_id=instance,
            anchor_set_id=f"set-{lineage}-{generation}-{instance}",
            anchors=(
                Anchor(AnchorKind.IDENTITY_ROOT, {"lineage_id": lineage}, anchor_id="identity", **common),
                Anchor(AnchorKind.CORE_PURPOSE, "purpose", anchor_id="purpose", **common),
                Anchor(AnchorKind.LIFE_RULE, "audit", anchor_id="rule", **common),
                Anchor(AnchorKind.LINEAGE_METADATA, {"lineage_id": lineage}, anchor_id="lineage", **common),
            ),
        )

    def test_anchor_set_append_roundtrip_and_generation_guard(self):
        with tempfile.TemporaryDirectory(prefix="anchor-vault-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            first = self._set()
            second = self._set(generation=1, instance="instance-vault-1")
            self.assertTrue(store.append_anchor_set(first))
            self.assertTrue(store.append_anchor_set(first))  # exact retry is idempotent
            self.assertFalse(store.append_anchor_set(self._set(instance="other-at-generation-0")))
            self.assertTrue(store.append_anchor_set(second))
            self.assertTrue(store.verify_anchor_vault())
            loaded = store.load_anchor_sets(lineage_id=first.lineage_id)
            self.assertEqual([item["set_hash"] for item in loaded], [first.set_hash, second.set_hash])
            typed = store.load_anchor_set_objects(lineage_id=first.lineage_id)
            self.assertEqual(typed[-1].to_dict(), second.to_dict())
            store.close()

    def test_anchor_vault_rejects_secret_set_and_physical_updates(self):
        with tempfile.TemporaryDirectory(prefix="anchor-vault-store-") as temp:
            path = str(Path(temp) / "brain.sqlite")
            init_db(path)
            store = StateStore(path)
            secret = self._set()
            secret_with_credential = AnchorSet(
                lineage_id=secret.lineage_id,
                generation=secret.generation,
                instance_id=secret.instance_id,
                anchor_set_id="secret-set",
                anchors=secret.anchors + (
                    Anchor(
                        AnchorKind.CREDENTIAL,
                        "DO-NOT-PERSIST",
                        anchor_id="credential",
                        source="trusted-observer",
                        verified=True,
                        trust_level=AnchorTrust.VERIFIED,
                        evidence_refs=("proof",),
                    ),
                ),
            )
            self.assertFalse(store.append_anchor_set(secret_with_credential))
            self.assertTrue(store.append_anchor_set(secret))
            conn = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("UPDATE anchor_vault SET set_hash='tampered'")
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("DELETE FROM anchor_vault")
                columns = [row[1].lower() for row in conn.execute("PRAGMA table_info(anchor_vault)")]
                self.assertFalse(any(token in column for column in columns for token in ("credential", "token", "password", "secret", "cookie")))
            finally:
                conn.close()
            self.assertTrue(store.verify_anchor_vault())
            self.assertNotIn("DO-NOT-PERSIST", json.dumps(store.load_anchor_sets(), ensure_ascii=False))
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
