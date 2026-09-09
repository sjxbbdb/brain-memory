"""Durable one-shot source-attestation tests.

These tests cover the host boundary that prevents a signed motivational
observation from being consumed again after a process restart.  The replay
ledger stores opaque hashes only; source labels and signed payloads remain in
the host process.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from brain.brain_stem import BrainStem
from brain.evolution import PromotionController, SandboxAttestor
from brain.evaluation_harness import EvaluationHarness
from brain.motivation import (
    ImpulseEvent,
    MotivationSourceAttestor,
    MotivationalPressure,
)
from storage.database import StateStore, init_db


class MotivationReplayTests(unittest.TestCase):
    _SECRET = b"durable-motivation-replay-test-secret"

    @staticmethod
    def _event(event_id: str = "durable-event-1") -> ImpulseEvent:
        return ImpulseEvent.create(
            "growth",
            0.9,
            "sensor-a",
            context="repeatable gap",
            source_kind="external",
            event_id=event_id,
            timestamp="100",
        )

    def test_proof_cannot_be_reused_after_attestor_restart(self):
        with tempfile.TemporaryDirectory(prefix="motivation-replay-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            first_store = StateStore(db_path)
            first = MotivationSourceAttestor(
                self._SECRET,
                issuer_id="test-host",
                clock=lambda: 100.0,
                replay_store=first_store,
            )
            event = self._event()
            proof = first.issue_for_event(
                event, source_id="sensor-a", now=100.0
            )
            pressure = MotivationalPressure(
                clock=lambda: 100.0,
                source_attestor=first,
                require_source_attestation=True,
            )
            self.assertTrue(
                pressure.accept(
                    ImpulseEvent.create(
                        "growth",
                        0.9,
                        "sensor-a",
                        context="repeatable gap",
                        source_kind="external",
                        event_id=event.event_id,
                        timestamp="100",
                        provenance=proof,
                    ),
                    now=100.0,
                )
            )
            first_store.close()

            # A new process would construct both objects again.  Reusing the
            # same signed proof must still fail because consumption is in the
            # SQLite transaction, not only the first attestor's memory.
            second_store = StateStore(db_path)
            second = MotivationSourceAttestor(
                self._SECRET,
                issuer_id="test-host",
                clock=lambda: 100.0,
                replay_store=second_store,
            )
            replay = MotivationalPressure(
                clock=lambda: 100.0,
                source_attestor=second,
                require_source_attestation=True,
            )
            self.assertFalse(
                replay.accept(
                    ImpulseEvent.create(
                        "growth",
                        0.9,
                        "sensor-a",
                        context="repeatable gap",
                        source_kind="external",
                        event_id=event.event_id,
                        timestamp="100",
                        provenance=proof,
                    ),
                    now=100.0,
                )
            )
            self.assertFalse(
                second.verify(
                    proof,
                    event_id=event.event_id,
                    payload_hash=MotivationalPressure.event_payload_hash(event),
                    source_kind="external",
                    now=100.0,
                )
            )
            second_store.close()

    def test_concurrent_consumers_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory(prefix="motivation-race-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            issuer = MotivationSourceAttestor(
                self._SECRET, issuer_id="test-host", clock=lambda: 100.0
            )
            event = self._event("durable-race")
            proof = issuer.issue_for_event(event, source_id="sensor-a", now=100.0)
            barrier = threading.Barrier(2)
            outcomes: list[bool] = []

            def consume() -> None:
                store = StateStore(db_path)
                attestor = MotivationSourceAttestor(
                    self._SECRET,
                    issuer_id="test-host",
                    clock=lambda: 100.0,
                    replay_store=store,
                )
                barrier.wait()
                try:
                    attestor.validate(
                        proof,
                        event_id=event.event_id,
                        payload_hash=MotivationalPressure.event_payload_hash(event),
                        source_kind="external",
                        now=100.0,
                    )
                except ValueError:
                    outcomes.append(False)
                else:
                    outcomes.append(True)
                finally:
                    store.close()

            threads = [threading.Thread(target=consume) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(outcomes), [False, True])

    def test_replay_table_is_append_only(self):
        with tempfile.TemporaryDirectory(prefix="motivation-guard-") as temp:
            db_path = str(Path(temp) / "brain.sqlite")
            init_db(db_path)
            store = StateStore(db_path)
            attestor = MotivationSourceAttestor(
                self._SECRET,
                issuer_id="test-host",
                clock=lambda: 100.0,
                replay_store=store,
            )
            event = self._event("durable-guard")
            proof = attestor.issue_for_event(event, source_id="sensor-a", now=100.0)
            attestor.validate(
                proof,
                event_id=event.event_id,
                payload_hash=MotivationalPressure.event_payload_hash(event),
                source_kind="external",
                now=100.0,
            )
            conn = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(
                        "UPDATE motivation_attestation_replay "
                        "SET consumed_at = consumed_at + 1"
                    )
                conn.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("DELETE FROM motivation_attestation_replay")
                conn.rollback()
            finally:
                conn.close()
            store.close()

    def test_strict_snapshot_requires_integrity_marker(self):
        attestor = MotivationSourceAttestor(
            self._SECRET, issuer_id="test-host", clock=lambda: 100.0
        )
        snapshot = MotivationalPressure(
            source_attestor=attestor,
            require_source_attestation=True,
            clock=lambda: 100.0,
        ).snapshot()
        snapshot.pop("integrity_hash")
        restored = MotivationalPressure.from_snapshot(
            snapshot,
            source_attestor=attestor,
            require_source_attestation=True,
            clock=lambda: 100.0,
        )
        self.assertTrue(restored.snapshot_rejected)
        self.assertEqual(restored.restore_error, "integrity_hash_missing")
        self.assertEqual(restored.events, ())

    def test_replay_storage_failure_never_falls_back_to_memory(self):
        with tempfile.TemporaryDirectory(prefix="motivation-replay-fail-") as temp:
            # A directory cannot be opened as a SQLite database file.  The
            # proof is valid, but strict admission must reject it rather than
            # silently treating the in-process cache as durable evidence.
            store = StateStore(temp)
            attestor = MotivationSourceAttestor(
                self._SECRET,
                issuer_id="test-host",
                clock=lambda: 100.0,
                replay_store=store,
            )
            base = self._event("durable-storage-failure")
            proof = attestor.issue_for_event(
                base, source_id="sensor-a", now=100.0
            )
            pressure = MotivationalPressure(
                source_attestor=attestor,
                require_source_attestation=True,
                clock=lambda: 100.0,
            )
            event = ImpulseEvent.create(
                "growth",
                0.9,
                "sensor-a",
                context="repeatable gap",
                source_kind="external",
                event_id=base.event_id,
                timestamp="100",
                provenance=proof,
            )
            self.assertFalse(pressure.accept(event, now=100.0))
            self.assertEqual(pressure.total_accepted, 0)

    def test_attestation_policy_properties_are_read_only(self):
        attestor = MotivationSourceAttestor(
            self._SECRET, issuer_id="test-host", clock=lambda: 100.0
        )
        pressure = MotivationalPressure(
            source_attestor=attestor,
            require_source_attestation=True,
            clock=lambda: 100.0,
        )
        with self.assertRaises(AttributeError):
            pressure.require_source_attestation = False
        with self.assertRaises(AttributeError):
            pressure.source_attestor = None
        with self.assertRaises(AttributeError):
            pressure.source_verifier = lambda *_: True

    def test_arbitrary_verifier_is_legacy_only(self):
        with self.assertRaises(TypeError):
            MotivationalPressure(
                require_source_attestation=True,
                source_verifier=lambda *_: True,
            )
        legacy = MotivationalPressure(
            require_source_attestation=False,
            source_verifier=lambda *_: True,
        )
        self.assertFalse(legacy.require_source_attestation)

    def test_production_iteration_rejects_non_durable_attestor(self):
        with tempfile.TemporaryDirectory(prefix="motivation-prod-") as temp:
            root = Path(temp)
            active = root / "active"
            fixtures = root / "fixtures"
            active.mkdir()
            fixtures.mkdir()
            (active / "program.txt").write_text("trusted", encoding="utf-8")
            (fixtures / "judge.py").write_text(
                'print("{\\"status\\":\\"pass\\",\\"verified\\":true,\\"metrics\\":{\\"quality\\":1}}")\n',
                encoding="utf-8",
            )
            harness = EvaluationHarness(
                fixtures=fixtures,
                command=["python", "{fixtures}/judge.py", "{candidate}"],
                primary_dimension="quality",
            )
            controller = PromotionController(
                active,
                harness=harness,
                protected_files=(),
                sandbox_attestor=SandboxAttestor(
                    secret=b"durable-promotion-attestor-secret"
                ),
                ledger_path=root / "promotion.jsonl",
                persistence_path=root / "brain.sqlite",
            )
            in_memory_only = MotivationSourceAttestor(
                self._SECRET, clock=lambda: 100.0
            )
            with self.assertRaises(PermissionError):
                BrainStem(
                    motivation_source_attestor=in_memory_only,
                    evaluation_harness=harness,
                    promotion_controller=controller,
                )

            init_db(str(root / "brain.sqlite"))
            store = StateStore(str(root / "brain.sqlite"))
            durable_attestor = MotivationSourceAttestor(
                self._SECRET,
                clock=lambda: 100.0,
                replay_store=store,
            )
            with self.assertRaises(PermissionError):
                BrainStem(
                    motivation_source_attestor=durable_attestor,
                    evaluation_harness=harness,
                    promotion_controller=controller,
                )
            stem = BrainStem(
                store,
                motivation_source_attestor=durable_attestor,
                evaluation_harness=harness,
                promotion_controller=controller,
            )
            self.assertTrue(
                stem.state.motivation.get("source_replay_durable", False)
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
