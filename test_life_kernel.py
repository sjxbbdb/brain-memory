"""TDD contract tests for the P1 LifeKernel seam.

These tests deliberately exercise the kernel through its public synchronous
interface.  They do not reach into BrainStem internals and they use a temporary
directory for the optional SQLite adapter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import asyncio
import sqlite3
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass
from pathlib import Path

from brain.life_kernel import (
    AppendOnlyLedger,
    IdentityCore,
    LedgerEvent,
    LedgerIntegrityError,
    LifeKernel,
    LifecycleError,
    LifecycleState,
    LifecycleTransitionError,
)
from brain.brain_stem import BrainStem
from storage.database import MemoryStore, StateStore, init_db

try:  # The P1 core may be used without a durable adapter in embedded mode.
    from brain.life_kernel import SQLiteLedger
except ImportError:  # pragma: no cover - optional adapter
    SQLiteLedger = None


def _value(value):
    """Normalize a str/Enum state without coupling tests to representation."""

    return getattr(value, "value", value)


def _state(kernel: LifeKernel):
    value = getattr(kernel, "current_state", None)
    if value is None:
        value = getattr(kernel, "state", None)
    return _value(value)


def _events(ledger):
    value = getattr(ledger, "events", ())
    if callable(value):
        value = value()
    return list(value or ())


def _event_dict(event):
    if isinstance(event, dict):
        return event
    serializer = getattr(event, "to_dict", None)
    if callable(serializer):
        return serializer()
    if is_dataclass(event):
        return asdict(event)
    return dict(vars(event))


def _chain_is_valid(ledger) -> bool:
    verifier = getattr(ledger, "verify_chain", None)
    if not callable(verifier):
        return True
    return bool(verifier())


class LifeKernelContractTests(unittest.TestCase):
    def test_identity_core_is_immutable(self):
        identity = IdentityCore()

        self.assertTrue(is_dataclass(identity))
        self.assertTrue(getattr(identity.__dataclass_params__, "frozen", False))
        self.assertGreater(len(fields(identity)), 0)

        # Try every declared field: a caller must not be able to rewrite the
        # constitutional identity after construction.
        for field in fields(identity):
            with self.subTest(field=field.name):
                with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
                    setattr(identity, field.name, "tampered")
                value = getattr(identity, field.name)
                if isinstance(value, list):
                    with self.assertRaises((AttributeError, TypeError)):
                        value.append("tampered")
                elif isinstance(value, dict):
                    with self.assertRaises((AttributeError, TypeError)):
                        value["tampered"] = True

    def test_default_kernel_is_created_with_stable_identity_ids(self):
        first = LifeKernel()
        second = LifeKernel()

        self.assertEqual(_state(first), _value(LifecycleState.CREATED))
        self.assertEqual(first.generation, 0)
        for name in ("lineage_id", "instance_id"):
            with self.subTest(name=name):
                self.assertTrue(str(getattr(first, name, "")).strip())
        # The constitutional identity root is represented by the immutable
        # IdentityCore object rather than duplicated as a mutable kernel field.
        self.assertTrue(str(first.identity_core.lineage_id).strip())
        self.assertNotEqual(first.instance_id, second.instance_id)
        # A fresh organism belongs to its own lineage unless explicitly
        # constructed as a successor.
        self.assertNotEqual(first.lineage_id, second.lineage_id)

    def test_valid_state_transitions_are_recorded(self):
        kernel = LifeKernel()
        path = (
            LifecycleState.BOOTSTRAPPING,
            LifecycleState.ACTIVE,
            LifecycleState.SLEEPING,
            LifecycleState.ACTIVE,
        )

        for target in path:
            event = kernel.transition(target, reason=f"test->{_value(target)}")
            self.assertIsInstance(event, LedgerEvent)
            self.assertEqual(_value(event.to_state), _value(target))

        self.assertEqual(_state(kernel), _value(LifecycleState.ACTIVE))
        events = _events(kernel.ledger)
        self.assertGreaterEqual(len(events), len(path))
        self.assertTrue(_chain_is_valid(kernel.ledger))

    def test_snapshot_parent_identity_is_pinned_by_genesis_metadata(self):
        kernel = LifeKernel()
        kernel.transition(LifecycleState.BOOTSTRAPPING, reason="boot")
        payload = kernel.snapshot()
        payload["identity"]["parent_instance_id"] = "forged-parent"
        with self.assertRaises(LedgerIntegrityError):
            LifeKernel.from_snapshot(payload)

    def test_invalid_transition_does_not_change_state(self):
        kernel = LifeKernel()

        # ACTIVE cannot be reached by skipping the bootstrapping state.
        with self.assertRaises(LifecycleTransitionError):
            kernel.transition(LifecycleState.ACTIVE, reason="illegal skip")

        self.assertEqual(_state(kernel), _value(LifecycleState.CREATED))
        self.assertTrue(_chain_is_valid(kernel.ledger))

        # If the convenience boolean seam is provided, it must have the same
        # fail-closed behavior rather than silently accepting the edge.
        try_transition = getattr(kernel, "try_transition", None)
        if callable(try_transition):
            self.assertFalse(
                try_transition(LifecycleState.ACTIVE, reason="illegal skip")
            )
            self.assertEqual(_state(kernel), _value(LifecycleState.CREATED))

    def test_recovery_rollback_returns_to_active(self):
        kernel = LifeKernel()
        kernel.transition(LifecycleState.BOOTSTRAPPING, reason="boot")
        kernel.transition(LifecycleState.ACTIVE, reason="ready")
        kernel.transition(LifecycleState.QUARANTINED, reason="integrity alarm")
        kernel.transition(LifecycleState.RECOVERING, reason="rollback baseline")
        kernel.transition(LifecycleState.ACTIVE, reason="recovery verified")

        self.assertEqual(_state(kernel), _value(LifecycleState.ACTIVE))
        self.assertTrue(_chain_is_valid(kernel.ledger))

    def test_terminal_state_cannot_be_revived(self):
        kernel = LifeKernel()
        kernel.transition(LifecycleState.DEAD, reason="irrecoverable integrity fault")
        self.assertEqual(_state(kernel), _value(LifecycleState.DEAD))

        with self.assertRaises(LifecycleTransitionError):
            kernel.transition(LifecycleState.ACTIVE, reason="attempted resurrection")
        self.assertEqual(_state(kernel), _value(LifecycleState.DEAD))

        # RETIRED is terminal as well, even though it is an orderly archive.
        retired = LifeKernel()
        retired.transition(LifecycleState.RETIRED, reason="orderly archive")
        with self.assertRaises(LifecycleTransitionError):
            retired.transition(LifecycleState.ACTIVE, reason="wake retired instance")
        self.assertEqual(_state(retired), _value(LifecycleState.RETIRED))

    def test_hash_chain_append_and_json_round_trip(self):
        kernel = LifeKernel()
        kernel.transition(LifecycleState.BOOTSTRAPPING, reason="boot")
        kernel.transition(LifecycleState.ACTIVE, reason="ready")
        kernel.transition(LifecycleState.DEGRADED, reason="resource budget")
        kernel.transition(LifecycleState.RECOVERING, reason="repair")

        ledger = kernel.ledger
        events = _events(ledger)
        self.assertGreaterEqual(len(events), 4)
        self.assertTrue(_chain_is_valid(ledger))
        sequence = []
        for event in events:
            data = _event_dict(event)
            if "seq" in data:
                sequence.append(int(data["seq"]))
            if "event_hash" in data:
                self.assertTrue(str(data["event_hash"]))
        if sequence:
            self.assertEqual(sequence, sorted(sequence))
            self.assertEqual(sequence, list(range(sequence[0], sequence[0] + len(sequence))))

        payload = json.loads(json.dumps(ledger.snapshot(), ensure_ascii=False))
        restored = AppendOnlyLedger.from_snapshot(payload)
        self.assertEqual(len(_events(restored)), len(events))
        self.assertTrue(_chain_is_valid(restored))
        self.assertEqual(
            [_event_dict(item) for item in _events(restored)],
            [_event_dict(item) for item in events],
        )

    def test_event_sink_failure_does_not_commit_state(self):
        calls = []

        def failing_sink(event):
            calls.append(event)
            raise OSError("durable ledger unavailable")

        kernel = LifeKernel()
        # The constructor records a genesis event before a sink is attached;
        # attach after construction with replay disabled so this test isolates
        # failure while committing a new transition.
        kernel.attach_event_sink(failing_sink, replay=False)
        before = _state(kernel)
        try:
            kernel.transition(LifecycleState.BOOTSTRAPPING, reason="sink failure")
        except Exception:
            # Either a typed persistence exception or the sink's original
            # exception is acceptable; the state transition is not.
            pass

        self.assertTrue(calls)
        self.assertEqual(_state(kernel), before)
        self.assertEqual(_state(kernel), _value(LifecycleState.CREATED))
        self.assertTrue(_chain_is_valid(kernel.ledger))


class BrainStemLifeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_restart_preserves_one_instance_lineage(self):
        """The durable lifecycle survives the bounded brain-state journal."""
        with tempfile.TemporaryDirectory(prefix="brain-life-stem-") as temp_dir:
            db_path = str(Path(temp_dir) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory)
            await stem.start()
            lineage_id = stem.life_kernel.lineage_id
            instance_id = stem.life_kernel.instance_id
            self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
            await asyncio.sleep(0.01)
            await stem.stop()
            self.assertEqual(stem.life_kernel.state, LifecycleState.SLEEPING)
            self.assertTrue(store.verify_life_ledger(instance_id=instance_id))
            events = store.load_life_events(instance_id=instance_id)
            self.assertEqual(
                [item["to_state"] for item in events],
                ["CREATED", "BOOTSTRAPPING", "ACTIVE", "SLEEPING"],
            )
            store.close()
            memory.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(restored_store, restored_memory)
            await restored.start()
            self.assertEqual(restored.life_kernel.lineage_id, lineage_id)
            self.assertEqual(restored.life_kernel.instance_id, instance_id)
            self.assertEqual(restored.life_kernel.state, LifecycleState.ACTIVE)
            self.assertTrue(restored_store.verify_life_ledger(instance_id=instance_id))
            await restored.stop()
            restored_store.close()
            restored_memory.close()

    async def test_terminal_kernel_rejects_input_and_wake(self):
        stem = BrainStem()
        stem.life_kernel.transition(
            LifecycleState.DEAD,
            reason="test terminal boundary",
            event_type="test_dead",
        )
        request_id, future = stem.submit_input("should not enter", source="test")
        result = await future
        self.assertEqual(result["request_id"], request_id)
        self.assertIn("terminal", result["llm_error_message"] or "")
        with self.assertRaises(LifecycleError):
            await stem.start()
        self.assertIsNone(stem._task)

    async def test_orderly_retirement_stops_runtime_and_is_not_wakeable(self):
        stem = BrainStem()
        await stem.start()
        self.assertEqual(stem.life_kernel.state, LifecycleState.ACTIVE)
        await stem.retire("test orderly retirement")
        self.assertEqual(stem.life_kernel.state, LifecycleState.RETIRED)
        self.assertIsNone(stem._task)
        with self.assertRaises(LifecycleError):
            await stem.start()

    async def test_death_is_terminal_and_rejects_new_input(self):
        stem = BrainStem()
        await stem.start()
        await stem.mark_dead("test integrity fault")
        self.assertEqual(stem.life_kernel.state, LifecycleState.DEAD)
        _, future = stem.submit_input("after death")
        result = await future
        self.assertFalse(result["accepted"])
        self.assertTrue(result["llm_error"])

    async def test_tampered_kernel_snapshot_blocks_restore(self):
        source = BrainStem()
        snapshot = source.life_kernel.snapshot()
        snapshot["identity_core"]["purpose"] = "tampered purpose"
        restored = BrainStem()
        with self.assertRaises(LifecycleError):
            restored._restore_life({"life_kernel": snapshot})
        self.assertTrue(restored._life_restore_blocked)
        self.assertTrue(restored.state.life.get("restore_blocked"))
        with self.assertRaises(LifecycleError):
            await restored.start()

    async def test_durable_ledger_can_restore_without_brain_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="brain-life-ledger-only-") as temp_dir:
            db_path = str(Path(temp_dir) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            memory = MemoryStore(db_path)
            stem = BrainStem(store, memory)
            await stem.start()
            lineage_id = stem.life_kernel.lineage_id
            instance_id = stem.life_kernel.instance_id
            await stem.stop()
            store.close()
            memory.close()
            # Simulate loss of the bounded snapshot journal while retaining
            # the permanent constitutional table.
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM brain_state")
                conn.commit()
            finally:
                conn.close()

            restored_store = StateStore(db_path)
            restored_memory = MemoryStore(db_path)
            restored = BrainStem(restored_store, restored_memory)
            await restored.start()
            self.assertEqual(restored.life_kernel.lineage_id, lineage_id)
            self.assertEqual(restored.life_kernel.instance_id, instance_id)
            self.assertEqual(restored.life_kernel.state, LifecycleState.ACTIVE)
            await restored.stop()
            restored_store.close()
            restored_memory.close()


@unittest.skipUnless(SQLiteLedger is not None, "SQLiteLedger adapter is optional")
class SQLiteLedgerContractTests(unittest.TestCase):
    def test_sqlite_ledger_survives_reopen_without_rewriting_history(self):
        with tempfile.TemporaryDirectory(prefix="brain-life-ledger-") as temp_dir:
            db_path = str(Path(temp_dir) / "life.sqlite")
            source = LifeKernel()
            source.transition(LifecycleState.BOOTSTRAPPING, reason="boot")
            source.transition(LifecycleState.ACTIVE, reason="ready")
            ledger = SQLiteLedger(db_path)
            for event in _events(source.ledger):
                self.assertTrue(ledger.append(event))
            before = [_event_dict(event) for event in _events(ledger)]
            self.assertTrue(_chain_is_valid(ledger))
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

            reopened = SQLiteLedger(db_path)
            after = [_event_dict(event) for event in _events(reopened)]
            self.assertEqual(after, before)
            self.assertTrue(_chain_is_valid(reopened))
            close = getattr(reopened, "close", None)
            if callable(close):
                close()

    def test_state_store_adapter_reloads_the_same_instance(self):
        with tempfile.TemporaryDirectory(prefix="brain-life-state-store-") as temp_dir:
            db_path = str(Path(temp_dir) / "life.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            source = LifeKernel()
            source.attach_event_sink(store.append_life_event)
            source.transition(LifecycleState.BOOTSTRAPPING, reason="boot")
            source.transition(LifecycleState.ACTIVE, reason="ready")

            restored = LifeKernel(
                ledger=store,
                instance_id=source.instance_id,
            )
            self.assertEqual(restored.lineage_id, source.lineage_id)
            self.assertEqual(restored.instance_id, source.instance_id)
            self.assertEqual(restored.state, LifecycleState.ACTIVE)
            self.assertEqual(restored.ledger.last_hash, source.ledger.last_hash)
            self.assertTrue(store.verify_life_ledger(instance_id=source.instance_id))
            store.close()

    def test_sqlite_adapter_rejects_gaps_and_illegal_edges_before_insert(self):
        with tempfile.TemporaryDirectory(prefix="brain-life-ledger-guard-") as temp_dir:
            db_path = str(Path(temp_dir) / "life.sqlite")
            source = LifeKernel()
            genesis = _events(source.ledger)[0]
            ledger = SQLiteLedger(db_path)
            self.assertTrue(ledger.append(genesis))

            # A sequence-3 event cannot be inserted while sequence-2 is
            # absent, even if its own hash is valid.
            gap = LedgerEvent.create(
                sequence=3,
                lineage_id=source.lineage_id,
                generation=source.generation,
                instance_id=source.instance_id,
                event_type="lifecycle_transition",
                from_state=LifecycleState.BOOTSTRAPPING,
                to_state=LifecycleState.ACTIVE,
                reason="gap",
                previous_hash=genesis.event_hash,
            )
            self.assertFalse(ledger.append(gap))

            # A correctly chained hash cannot bypass the lifecycle transition
            # matrix (CREATED -> ACTIVE is not a legal edge).
            illegal = LedgerEvent.create(
                sequence=2,
                lineage_id=source.lineage_id,
                generation=source.generation,
                instance_id=source.instance_id,
                event_type="lifecycle_transition",
                from_state=LifecycleState.CREATED,
                to_state=LifecycleState.ACTIVE,
                reason="illegal skip",
                previous_hash=genesis.event_hash,
            )
            self.assertFalse(ledger.append(illegal))
            self.assertEqual(len(ledger.events), 1)

    def test_sqlite_table_is_insert_only_even_through_raw_connection(self):
        with tempfile.TemporaryDirectory(prefix="brain-life-ledger-guard-") as temp_dir:
            db_path = str(Path(temp_dir) / "life.sqlite")
            source = LifeKernel()
            ledger = SQLiteLedger(db_path)
            self.assertTrue(ledger.append(_events(source.ledger)[0]))
            conn = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("UPDATE life_ledger SET reason = 'tampered'")
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("DELETE FROM life_ledger")
                conn.rollback()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
