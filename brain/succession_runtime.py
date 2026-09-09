"""Fail-closed lifecycle coordination for disaster-only succession.

The data policy in :mod:`brain.succession` deliberately cannot start or stop
an organism.  This module is the narrow runtime seam that joins that policy to
the immutable :class:`brain.life_kernel.LifeKernel` state machine.

Succession is intentionally conservative:

* a recovery-eligible incident cannot create a new generation;
* the parent is quarantined and moved through ``SUCCESSION_PENDING`` before a
  successor is exposed;
* hard integrity/security failures end in ``DEAD`` while confirmed drift that
  survived recovery ends in ``RETIRED``;
* the successor starts in ``CREATED`` and receives no environment capability;
* only a redacted, independently hash-pinned inheritance plan crosses the
  boundary; and
* persistence failures leave the lineage without an exposed successor rather
  than allowing two controlling instances.

This is not process cloning and it is not an OS sandbox.  Activation and
environment binding remain separate, host-owned operations after independent
evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from threading import RLock
import time
from typing import Any, Callable, Iterable, Mapping
import uuid

from brain.life_kernel import (
    LifeKernel,
    LifecycleError,
    LifecycleState,
)
from brain.succession import (
    AnchorSet,
    AnchorKind,
    AnchorVault,
    FailureAssessment,
    FailureClass,
    InheritancePlan,
    SuccessionError,
    SuccessionLedger,
    SuccessionRecord,
    filter_inheritance,
)


SUCCESSION_RUNTIME_SCHEMA_VERSION = 1
ACTIVATION_ATTESTATION_SCHEMA_VERSION = 1
ACTIVATION_ATTESTATION_MAX_TTL_SEC = 60 * 60
_TERMINAL_STATES = frozenset({LifecycleState.RETIRED, LifecycleState.DEAD})
_HARD_FAILURES = frozenset(
    {FailureClass.HARD_INTEGRITY_FAILURE, FailureClass.SECURITY_BREACH}
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _snapshot_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _normalise_activation_profile(value: Any) -> str:
    """Normalize the small host policy vocabulary in one place."""

    text = _activation_text(value, 40).strip().lower() or "production"
    aliases = {
        "prod": "production",
        "strict": "production",
        "host": "production",
        "test": "legacy",
        "dev": "legacy",
        "local": "legacy",
    }
    text = aliases.get(text, text)
    if text not in {"production", "legacy"}:
        raise TypeError("succession profile must be 'production' or explicit 'legacy'")
    return text


def _activation_text(value: Any, limit: int = 500) -> str:
    """Bound text that crosses the host-issued activation proof boundary."""

    try:
        text = "" if value is None else str(value)
    except Exception:
        text = ""
    return "".join(
        character
        if ord(character) >= 32 or character in "\r\n\t"
        else " "
        for character in text
    )[:limit]


def _activation_epoch(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        result = parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise SuccessionRuntimeError("activation attestation timestamp is invalid") from exc
    if not (result == result) or result in {float("inf"), float("-inf")}:
        raise SuccessionRuntimeError("activation attestation timestamp is not finite")
    return result


def _activation_iso(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _activation_sha(value: Any) -> str:
    normalized = _activation_text(value, 128).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise SuccessionRuntimeError("activation attestation hash is malformed")
    return normalized


def _call_sink(
    sink: Callable[[dict[str, Any]], Any] | None,
    payload: dict[str, Any],
    *,
    label: str,
) -> None:
    if sink is None:
        return
    try:
        result = sink(payload)
    except Exception as exc:  # pragma: no cover - adapter-specific exception
        raise SuccessionRuntimeError(f"{label} persistence failed") from exc
    if result is not None and not bool(result):
        raise SuccessionRuntimeError(f"{label} persistence rejected the record")


class SuccessionRuntimeError(SuccessionError):
    """A succession could not safely cross the lifecycle boundary."""


@dataclass(frozen=True, slots=True)
class SuccessionOutcome:
    """Immutable references describing one completed handover.

    ``successor`` is deliberately still ``CREATED``.  The outcome grants no
    permission to bootstrap it and contains no credential, session or action
    capability.
    """

    parent: LifeKernel
    successor: LifeKernel
    failure: FailureAssessment
    inheritance: InheritancePlan
    successor_anchor_set: AnchorSet
    record: SuccessionRecord

    @property
    def lineage_id(self) -> str:
        return self.successor.lineage_id

    @property
    def parent_instance_id(self) -> str:
        return self.parent.instance_id

    @property
    def successor_instance_id(self) -> str:
        return self.successor.instance_id

    def verify(self) -> bool:
        """Verify the complete parent/child/anchor/record boundary.

        Each component is independently hash-pinned, but a valid object from
        another lineage or generation must not be graftable onto a valid
        runtime snapshot.  The cross-object checks live at this narrow public
        seam so normal handovers and restart reconstruction share one proof.
        """

        self.parent.ledger.verify_chain()
        self.successor.ledger.verify_chain()
        self.failure.verify()
        self.inheritance.verify()
        self.successor_anchor_set.verify()
        self.record.verify()
        if not self.parent.is_terminal:
            raise SuccessionRuntimeError("completed succession has a non-terminal parent")
        self._verify_bindings()
        # The handover creates the child in CREATED, but a host may later move
        # it through BOOTSTRAPPING/ACTIVE (or a safe degraded/recovery path).
        # Verification therefore checks the identity boundary rather than
        # freezing the child forever at its birth state.
        if self.successor.state == LifecycleState.SUCCESSION_PENDING:
            raise SuccessionRuntimeError(
                "successor may not enter SUCCESSION_PENDING under its parent record"
            )
        if self.successor.identity_core.fingerprint != self.parent.identity_core.fingerprint:
            raise SuccessionRuntimeError("successor identity core differs from parent")
        if self.successor.generation != self.parent.generation + 1:
            raise SuccessionRuntimeError("successor generation is not parent + 1")
        if self.successor.parent_instance_id != self.parent.instance_id:
            raise SuccessionRuntimeError("successor does not name its parent")
        self._verify_activation_evidence(self.successor)
        return True

    def _verify_bindings(self) -> bool:
        """Verify relationships without requiring the parent to be terminal.

        ``SuccessionCoordinator.succeed`` invokes this before the irreversible
        parent freeze.  ``verify`` invokes it again after durable sinks and
        in-memory ledgers have been attached.
        """

        parent = self.parent
        child = self.successor
        plan = self.inheritance
        record = self.record
        child_anchors = self.successor_anchor_set

        # Every identity/generation edge must agree.  A hash-valid anchor set
        # from another lineage is not a valid successor merely because its
        # instance_id happens to be reused.
        if parent.lineage_id != child.lineage_id:
            raise SuccessionRuntimeError("successor lineage differs from parent")
        if plan.lineage_id != parent.lineage_id or record.lineage_id != parent.lineage_id:
            raise SuccessionRuntimeError("succession lineage binding is inconsistent")
        if plan.parent_instance_id != parent.instance_id:
            raise SuccessionRuntimeError("inheritance source names another parent")
        if plan.parent_generation != parent.generation:
            raise SuccessionRuntimeError("inheritance source generation differs from parent")
        if plan.successor_generation != child.generation:
            raise SuccessionRuntimeError("inheritance successor generation differs from child")
        if record.parent_instance_id != parent.instance_id:
            raise SuccessionRuntimeError("succession record names another parent")
        if record.parent_generation != parent.generation:
            raise SuccessionRuntimeError("succession record parent generation differs")
        if record.successor_instance_id != child.instance_id:
            raise SuccessionRuntimeError("succession record names another successor")
        if record.successor_generation != child.generation:
            raise SuccessionRuntimeError("succession record successor generation differs")

        # Do not mix an independently supplied failure or plan with the copies
        # embedded in the immutable record.
        if record.failure.assessment_hash != self.failure.assessment_hash:
            raise SuccessionRuntimeError("succession failure binding is inconsistent")
        if record.inheritance.plan_hash != plan.plan_hash:
            raise SuccessionRuntimeError("succession inheritance binding is inconsistent")

        if child_anchors.lineage_id != parent.lineage_id:
            raise SuccessionRuntimeError("successor anchors belong to another lineage")
        if child_anchors.generation != child.generation:
            raise SuccessionRuntimeError("successor anchors belong to another generation")
        if child_anchors.generation != plan.successor_generation:
            raise SuccessionRuntimeError("successor anchor generation differs from plan")
        if child_anchors.instance_id != child.instance_id:
            raise SuccessionRuntimeError("successor anchors name another instance")
        if child_anchors.instance_id != record.successor_instance_id:
            raise SuccessionRuntimeError("successor anchors disagree with succession record")

        # ``build_successor_anchor_set`` writes these provenance pins.  Check
        # both values and the copied anchor payload, not just the set's own
        # content hash, so a valid but unrelated set cannot be substituted.
        metadata = dict(child_anchors.metadata)
        if metadata.get("inherited_from") != plan.source_anchor_set_id:
            raise SuccessionRuntimeError("successor anchors lose source-set provenance")
        if metadata.get("inheritance_plan_hash") != plan.plan_hash:
            raise SuccessionRuntimeError("successor anchors lose inheritance-plan provenance")
        expected_anchors = _canonical_json(
            [anchor.to_dict() for anchor in plan.inherited_anchors]
        )
        actual_anchors = _canonical_json(
            [anchor.to_dict() for anchor in child_anchors.anchors]
        )
        if actual_anchors != expected_anchors:
            raise SuccessionRuntimeError(
                "successor anchor payload differs from the inheritance plan"
            )

        if len(child_anchors.set_hash) != 64 or len(plan.plan_hash) != 64:
            raise SuccessionRuntimeError("successor continuity hashes are malformed")
        # If the child has crossed the activation gate, its lifecycle events
        # must point back to this exact immutable record and plan.  This makes
        # the event metadata useful during restart verification rather than a
        # free-form note that can be copied from another child.
        if child.state not in {
            LifecycleState.CREATED,
            LifecycleState.RETIRED,
            LifecycleState.DEAD,
        }:
            expected_activation = {
                "succession_record_id": record.record_id,
                "succession_record_hash": record.record_hash,
                "inheritance_plan_hash": plan.plan_hash,
            }
            for event in child.ledger.events:
                if event.event_type in {
                    "successor_bootstrap_started",
                    "successor_activated",
                }:
                    metadata = dict(event.metadata)
                    for key, expected in expected_activation.items():
                        if str(metadata.get(key, "")).strip().lower() != str(expected).lower():
                            raise SuccessionRuntimeError(
                                f"successor activation {key} does not match succession boundary"
                            )
        return True

    @staticmethod
    def _verify_activation_evidence(child: LifeKernel) -> str | None:
        """Require an auditable evaluator receipt for a live successor.

        A durable child can outlive the bounded runtime snapshot.  Seeing an
        ``ACTIVE`` (or other non-terminal) state in a hash-valid ledger is not
        by itself proof that the activation gate was passed: a partial sink
        write or a hand-built snapshot could otherwise bypass evaluation on
        restart.  Terminal children are safe audit artifacts and cannot own
        an environment, so they may be sealed before activation.
        """

        if child.state in {
            LifecycleState.CREATED,
            LifecycleState.RETIRED,
            LifecycleState.DEAD,
        }:
            return None
        activated = [
            event
            for event in child.ledger.events
            if event.event_type == "successor_activated"
        ]
        if not activated:
            raise SuccessionRuntimeError(
                "non-terminal successor lacks an independent activation event"
            )
        receipt_hashes: list[str] = []
        boundary_refs: list[tuple[str, str, str]] = []
        attestation_refs: list[tuple[str, str]] = []
        valid_hex = set("0123456789abcdef")
        for event in activated:
            if (
                event.from_state != LifecycleState.BOOTSTRAPPING.value
                or event.to_state != LifecycleState.ACTIVE.value
            ):
                raise SuccessionRuntimeError(
                    "successor activation event has an invalid lifecycle edge"
                )
            metadata = dict(event.metadata)
            value = str(metadata.get("evaluation_receipt_hash", "")).strip().lower()
            if len(value) != 64 or any(char not in valid_hex for char in value):
                raise SuccessionRuntimeError(
                    "successor activation event lacks a hash-pinned evaluation receipt"
                )
            receipt_hashes.append(value)
            metadata = dict(event.metadata)
            record_id = str(metadata.get("succession_record_id", "")).strip()
            record_hash = str(metadata.get("succession_record_hash", "")).strip().lower()
            plan_hash = str(metadata.get("inheritance_plan_hash", "")).strip().lower()
            if (
                not record_id
                or not re.fullmatch(r"[0-9a-f]{64}", record_hash)
                or not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
            ):
                raise SuccessionRuntimeError(
                    "successor activation event lacks succession boundary bindings"
                )
            boundary_refs.append((record_id, record_hash, plan_hash))
            attestation_id = str(metadata.get("activation_attestation_id", "")).strip()
            attestation_hash = str(
                metadata.get("activation_attestation_hash", "")
            ).strip().lower()
            if attestation_id or attestation_hash:
                if not attestation_id or not re.fullmatch(
                    r"[0-9a-f]{64}", attestation_hash
                ):
                    raise SuccessionRuntimeError(
                        "successor activation attestation metadata is malformed"
                    )
                nested = metadata.get("activation_attestation")
                if not isinstance(nested, Mapping):
                    raise SuccessionRuntimeError(
                        "successor activation attestation payload is missing"
                    )
                try:
                    parsed_attestation = SuccessorActivationAttestation.from_dict(nested)
                except Exception as exc:
                    raise SuccessionRuntimeError(
                        "successor activation attestation payload is malformed"
                    ) from exc
                if (
                    parsed_attestation.attestation_id != attestation_id
                    or parsed_attestation.attestation_hash != attestation_hash
                ):
                    raise SuccessionRuntimeError(
                        "successor activation attestation metadata disagrees with payload"
                    )
                attestation_refs.append((attestation_id, attestation_hash))
        if len(set(receipt_hashes)) != 1:
            raise SuccessionRuntimeError("successor activation receipt hashes disagree")
        if len(set(boundary_refs)) != 1:
            raise SuccessionRuntimeError(
                "successor activation boundary bindings disagree"
            )
        if attestation_refs and len(set(attestation_refs)) != 1:
            raise SuccessionRuntimeError(
                "successor activation attestation bindings disagree"
            )
        for event in child.ledger.events:
            if event.event_type == "successor_bootstrap_started":
                metadata = dict(event.metadata)
                value = str(metadata.get("evaluation_receipt_hash", "")).strip().lower()
                if value != receipt_hashes[0]:
                    raise SuccessionRuntimeError(
                        "successor bootstrap and activation receipt hashes disagree"
                    )
                if (
                    str(metadata.get("succession_record_id", "")).strip()
                    != boundary_refs[0][0]
                    or str(metadata.get("succession_record_hash", "")).strip().lower()
                    != boundary_refs[0][1]
                    or str(metadata.get("inheritance_plan_hash", "")).strip().lower()
                    != boundary_refs[0][2]
                ):
                    raise SuccessionRuntimeError(
                        "successor bootstrap and activation boundary bindings disagree"
                    )
        return receipt_hashes[0]

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted audit projection, not executable state."""

        return {
            "schema_version": SUCCESSION_RUNTIME_SCHEMA_VERSION,
            "lineage_id": self.lineage_id,
            "parent": self.parent.public_snapshot(),
            "successor": self.successor.public_snapshot(),
            "failure": self.failure.to_dict(),
            "inheritance": self.inheritance.to_dict(),
            "successor_anchor_set": self.successor_anchor_set.to_dict(),
            "record": self.record.to_dict(),
            "activation_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class SuccessorActivationAttestation:
    """Host-issued proof binding one evaluator receipt to one successor.

    ``EvaluationReceipt`` proves that a candidate passed the fixed evaluator;
    it does not identify *which* succession boundary may consume it.  This
    separate, opaque proof closes that grafting gap by signing the complete
    parent/child/record/plan tuple together with the exact receipt contract.
    The signing secret never enters a snapshot or a child-visible payload.
    """

    attestation_id: str
    issuer_id: str
    lineage_id: str
    parent_instance_id: str
    parent_generation: int
    successor_instance_id: str
    successor_generation: int
    succession_record_id: str
    succession_record_hash: str
    inheritance_plan_hash: str
    receipt_hash: str
    candidate_revision_id: str
    candidate_fingerprint: str
    mode: str
    harness_version: str
    fixture_hash: str
    evaluator_hash: str
    issued_at: str
    expires_at: str
    nonce: str
    signature: str

    def __post_init__(self) -> None:
        for name, limit in (
            ("attestation_id", 160),
            ("issuer_id", 160),
            ("lineage_id", 160),
            ("parent_instance_id", 160),
            ("successor_instance_id", 160),
            ("succession_record_id", 200),
            ("candidate_revision_id", 200),
            ("mode", 40),
            ("harness_version", 100),
            ("issued_at", 100),
            ("expires_at", 100),
            ("nonce", 180),
            ("signature", 128),
        ):
            object.__setattr__(
                self,
                name,
                _activation_text(getattr(self, name), limit).strip(),
            )
        for name in ("parent_generation", "successor_generation"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise SuccessionRuntimeError(f"{name} must be an integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SuccessionRuntimeError(f"{name} must be an integer") from exc
            if parsed < 0:
                raise SuccessionRuntimeError(f"{name} must be non-negative")
            object.__setattr__(self, name, parsed)
        if not self.attestation_id or not self.issuer_id or not self.lineage_id:
            raise SuccessionRuntimeError("activation attestation identity is incomplete")
        if not self.parent_instance_id or not self.successor_instance_id:
            raise SuccessionRuntimeError("activation attestation instances are incomplete")
        if not self.succession_record_id or not self.candidate_revision_id:
            raise SuccessionRuntimeError("activation attestation references are incomplete")
        for name in (
            "succession_record_hash",
            "inheritance_plan_hash",
            "receipt_hash",
            "candidate_fingerprint",
            "fixture_hash",
            "evaluator_hash",
        ):
            object.__setattr__(self, name, _activation_sha(getattr(self, name)))
        mode = _activation_text(self.mode, 40).strip().lower()
        if mode not in {"evolution", "recovery"}:
            raise SuccessionRuntimeError("activation attestation mode is invalid")
        object.__setattr__(self, "mode", mode)
        if not self.harness_version:
            raise SuccessionRuntimeError("activation attestation harness version is missing")
        issued = _activation_epoch(self.issued_at)
        expires = _activation_epoch(self.expires_at)
        if expires <= issued:
            raise SuccessionRuntimeError("activation attestation expiry is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signature.lower()):
            raise SuccessionRuntimeError("activation attestation signature is invalid")
        object.__setattr__(self, "signature", self.signature.lower())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": ACTIVATION_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": self.attestation_id,
            "issuer_id": self.issuer_id,
            "lineage_id": self.lineage_id,
            "parent_instance_id": self.parent_instance_id,
            "parent_generation": self.parent_generation,
            "successor_instance_id": self.successor_instance_id,
            "successor_generation": self.successor_generation,
            "succession_record_id": self.succession_record_id,
            "succession_record_hash": self.succession_record_hash,
            "inheritance_plan_hash": self.inheritance_plan_hash,
            "receipt_hash": self.receipt_hash,
            "candidate_revision_id": self.candidate_revision_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "mode": self.mode,
            "harness_version": self.harness_version,
            "fixture_hash": self.fixture_hash,
            "evaluator_hash": self.evaluator_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    def signing_payload(self) -> bytes:
        return _canonical_json(self._payload()).encode("utf-8")

    @property
    def attestation_hash(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict()).encode("utf-8")
        ).hexdigest()

    @property
    def hash(self) -> str:
        return self.attestation_hash

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SuccessorActivationAttestation":
        if not isinstance(data, Mapping):
            raise SuccessionRuntimeError("activation attestation must be an object")
        payload = dict(data)
        payload.pop("schema_version", None)
        try:
            return cls(**payload)
        except SuccessionRuntimeError:
            raise
        except (TypeError, ValueError) as exc:
            raise SuccessionRuntimeError("invalid activation attestation payload") from exc

    def verify_signature(self, secret: bytes) -> bool:
        if not isinstance(secret, (bytes, bytearray)):
            return False
        expected = hmac.new(
            bytes(secret), self.signing_payload(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def is_expired(self, *, now: float | None = None, skew_sec: float = 0.0) -> bool:
        current = time.time() if now is None else float(now)
        return current > _activation_epoch(self.expires_at) + float(skew_sec)


class SuccessorActivationAttestor:
    """Trusted host issuer/one-time verifier for successor activation proofs."""

    def __init__(
        self,
        *,
        issuer_id: str | None = None,
        secret: bytes | bytearray | None = None,
        clock: Any = None,
        max_ttl_sec: float = ACTIVATION_ATTESTATION_MAX_TTL_SEC,
    ) -> None:
        if secret is None:
            secret = secrets.token_bytes(32)
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise SuccessionRuntimeError(
                "activation attestor secret must be at least 16 bytes"
            )
        self._secret = bytes(secret)
        self.issuer_id = (
            _activation_text(issuer_id, 160).strip()
            or f"activation-host-{uuid.uuid4().hex}"
        )
        self._clock = clock or time.time
        try:
            self.max_ttl_sec = float(max_ttl_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SuccessionRuntimeError("activation max_ttl_sec is invalid") from exc
        if not (self.max_ttl_sec > 0 and self.max_ttl_sec < float("inf")):
            raise SuccessionRuntimeError("activation max_ttl_sec must be positive")
        self._used_ids: set[str] = set()
        self._lock = RLock()

    @staticmethod
    def _receipt_details(receipt: Any) -> tuple[Any, str]:
        """Require the fixed evaluator type, never a child-owned look-alike."""

        try:
            from brain.evaluation_harness import EvaluationReceipt

            if isinstance(receipt, Mapping):
                receipt = EvaluationReceipt.from_dict(receipt)
            if type(receipt) is not EvaluationReceipt or not receipt.verify():
                raise SuccessionRuntimeError(
                    "activation attestation requires a verified EvaluationReceipt"
                )
        except SuccessionRuntimeError:
            raise
        except Exception as exc:
            raise SuccessionRuntimeError(
                "activation evaluation receipt is malformed"
            ) from exc
        # Reuse the lifecycle seam's hard-gate checks, including the strict
        # lowercase SHA-256 receipt hash validation.
        SuccessionCoordinator._verified_evaluation_receipt(receipt)
        return receipt, receipt.receipt_hash

    def issue(
        self,
        outcome: SuccessionOutcome,
        evaluation_receipt: Any,
        *,
        ttl_sec: float | None = None,
    ) -> SuccessorActivationAttestation:
        if not isinstance(outcome, SuccessionOutcome):
            raise SuccessionRuntimeError("activation outcome must be SuccessionOutcome")
        outcome.verify()
        receipt, receipt_hash = self._receipt_details(evaluation_receipt)
        try:
            ttl = self.max_ttl_sec if ttl_sec is None else float(ttl_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SuccessionRuntimeError("activation ttl_sec is invalid") from exc
        if not (ttl > 0 and ttl <= self.max_ttl_sec and ttl < float("inf")):
            raise SuccessionRuntimeError("activation ttl_sec is outside the allowed range")
        now = float(self._clock())
        if not (now == now) or now in {float("inf"), float("-inf")}:
            raise SuccessionRuntimeError("activation host clock is invalid")
        payload = {
            "schema_version": ACTIVATION_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": f"succ-att-{uuid.uuid4().hex}",
            "issuer_id": self.issuer_id,
            "lineage_id": outcome.lineage_id,
            "parent_instance_id": outcome.parent.instance_id,
            "parent_generation": outcome.parent.generation,
            "successor_instance_id": outcome.successor.instance_id,
            "successor_generation": outcome.successor.generation,
            "succession_record_id": outcome.record.record_id,
            "succession_record_hash": outcome.record.record_hash,
            "inheritance_plan_hash": outcome.inheritance.plan_hash,
            "receipt_hash": receipt_hash,
            "candidate_revision_id": receipt.candidate_revision_id,
            "candidate_fingerprint": receipt.candidate_fingerprint,
            "mode": receipt.mode,
            "harness_version": receipt.harness_version,
            "fixture_hash": receipt.fixture_hash,
            "evaluator_hash": receipt.evaluator_hash,
            "issued_at": _activation_iso(now),
            "expires_at": _activation_iso(now + ttl),
            "nonce": secrets.token_urlsafe(24),
        }
        payload["signature"] = hmac.new(
            self._secret,
            _canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return SuccessorActivationAttestation.from_dict(payload)

    issue_for_outcome = issue
    attest = issue

    def validate(
        self,
        value: SuccessorActivationAttestation | Mapping[str, Any],
        *,
        outcome: SuccessionOutcome,
        evaluation_receipt: Any,
        consume: bool = False,
        now: float | None = None,
    ) -> SuccessorActivationAttestation:
        if isinstance(value, SuccessorActivationAttestation):
            attestation = value
        elif isinstance(value, Mapping):
            attestation = SuccessorActivationAttestation.from_dict(value)
        else:
            raise SuccessionRuntimeError("activation proof must be host-issued")
        if not isinstance(outcome, SuccessionOutcome):
            raise SuccessionRuntimeError("activation outcome is invalid")
        outcome.verify()
        receipt, receipt_hash = self._receipt_details(evaluation_receipt)
        with self._lock:
            if attestation.issuer_id != self.issuer_id:
                raise SuccessionRuntimeError("activation attestation issuer is not trusted")
            if attestation.attestation_id in self._used_ids:
                raise SuccessionRuntimeError("activation attestation was already consumed")
            if not attestation.verify_signature(self._secret):
                raise SuccessionRuntimeError("activation attestation signature mismatch")
            current = float(self._clock()) if now is None else float(now)
            if not (current == current) or current in {float("inf"), float("-inf")}:
                raise SuccessionRuntimeError("activation attestation clock is invalid")
            if attestation.is_expired(now=current, skew_sec=5.0):
                raise SuccessionRuntimeError("activation attestation is expired")
            if current + 5.0 < _activation_epoch(attestation.issued_at):
                raise SuccessionRuntimeError("activation attestation is not yet valid")
            expected = {
                "lineage_id": outcome.lineage_id,
                "parent_instance_id": outcome.parent.instance_id,
                "parent_generation": outcome.parent.generation,
                "successor_instance_id": outcome.successor.instance_id,
                "successor_generation": outcome.successor.generation,
                "succession_record_id": outcome.record.record_id,
                "succession_record_hash": outcome.record.record_hash,
                "inheritance_plan_hash": outcome.inheritance.plan_hash,
                "receipt_hash": receipt_hash,
                "candidate_revision_id": receipt.candidate_revision_id,
                "candidate_fingerprint": receipt.candidate_fingerprint,
                "mode": receipt.mode,
                "harness_version": receipt.harness_version,
                "fixture_hash": receipt.fixture_hash,
                "evaluator_hash": receipt.evaluator_hash,
            }
            for name, wanted in expected.items():
                if str(getattr(attestation, name)) != str(wanted):
                    raise SuccessionRuntimeError(
                        f"activation attestation {name} binding mismatch"
                    )
            if consume:
                self._used_ids.add(attestation.attestation_id)
            return attestation

    def validate_bound(
        self,
        value: SuccessorActivationAttestation | Mapping[str, Any],
        *,
        outcome: SuccessionOutcome,
        now: float | None = None,
    ) -> SuccessorActivationAttestation:
        """Verify a persisted proof against an outcome without a new receipt.

        Restart recovery may have only the signed proof and the immutable
        child ledger.  The receipt hash and evaluator contract are already
        embedded in the proof; the host can require a fresh receipt at the
        activation call, while this method still checks the HMAC, expiry and
        complete succession boundary before trusting an already-ACTIVE child.
        It deliberately does not consume the nonce, making read-only verify
        and restart checks idempotent.
        """

        if isinstance(value, SuccessorActivationAttestation):
            attestation = value
        elif isinstance(value, Mapping):
            attestation = SuccessorActivationAttestation.from_dict(value)
        else:
            raise SuccessionRuntimeError("persisted activation proof is invalid")
        if not isinstance(outcome, SuccessionOutcome):
            raise SuccessionRuntimeError("activation outcome is invalid")
        with self._lock:
            if attestation.issuer_id != self.issuer_id:
                raise SuccessionRuntimeError("activation attestation issuer is not trusted")
            if not attestation.verify_signature(self._secret):
                raise SuccessionRuntimeError("activation attestation signature mismatch")
            current = float(self._clock()) if now is None else float(now)
            if not (current == current) or current in {float("inf"), float("-inf")}:
                raise SuccessionRuntimeError("activation attestation clock is invalid")
            if attestation.is_expired(now=current, skew_sec=5.0):
                raise SuccessionRuntimeError("activation attestation is expired")
            if current + 5.0 < _activation_epoch(attestation.issued_at):
                raise SuccessionRuntimeError("activation attestation is not yet valid")
            expected = {
                "lineage_id": outcome.lineage_id,
                "parent_instance_id": outcome.parent.instance_id,
                "parent_generation": outcome.parent.generation,
                "successor_instance_id": outcome.successor.instance_id,
                "successor_generation": outcome.successor.generation,
                "succession_record_id": outcome.record.record_id,
                "succession_record_hash": outcome.record.record_hash,
                "inheritance_plan_hash": outcome.inheritance.plan_hash,
            }
            for name, wanted in expected.items():
                if str(getattr(attestation, name)) != str(wanted):
                    raise SuccessionRuntimeError(
                        f"persisted activation attestation {name} binding mismatch"
                    )
            return attestation

    def verify(self, value: Any, **kwargs: Any) -> bool:
        try:
            self.validate(value, **kwargs)
            return True
        except (SuccessionRuntimeError, TypeError, ValueError):
            return False

    @property
    def consumed_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._used_ids)


class SuccessionCoordinator:
    """Serialize one irreversible parent-to-successor lifecycle boundary.

    The coordinator owns no process, network, tool or filesystem capability.
    Optional sinks are host-controlled append functions.  It is safe to omit
    them for an in-memory evaluation; a production host should supply durable
    sinks and treat any rejection as a fail-closed incident.
    """

    __slots__ = (
        "_parent",
        "_successor",
        "_outcome",
        "_anchor_vault",
        "_succession_ledger",
        "_life_event_sink",
        "_anchor_sink",
        "_record_sink",
        "_activation_profile",
        "_require_activation_attestation",
        "_activation_attestor",
        "_activation_attestation",
        "_lock",
    )

    # A process-local control registry closes the most common split-brain
    # failure: two independently constructed coordinators for the same
    # lineage both believing they own the active instance.  Durable hosts
    # should additionally enforce a database/OS lease; this registry is a
    # last in-process guard and deliberately stores no credentials.
    _registry_lock = RLock()
    # The optional owner token is process-local and opaque.  Legacy callers
    # that omit it retain the old idempotent API, while BrainStem instances
    # pass their own token so one stem cannot release another stem's guard
    # when they happen to share the same LifeKernel object.
    _control_registry: dict[str, tuple[str, LifeKernel, object | None]] = {}

    @classmethod
    def _purge_terminal_registry(cls) -> None:
        stale = [
            lineage
            for lineage, (_instance_id, kernel, _owner) in cls._control_registry.items()
            if kernel.is_terminal
        ]
        for lineage in stale:
            cls._control_registry.pop(lineage, None)

    @classmethod
    def claim_control(cls, kernel: LifeKernel, *, owner: object | None = None) -> None:
        """Claim one non-terminal lineage instance for this process.

        Re-claiming the same object/instance is idempotent (needed when a
        BrainStem restart reattaches its coordinator).  A different
        non-terminal instance in the same lineage is always rejected.
        """

        if not isinstance(kernel, LifeKernel):
            raise TypeError("kernel must be a LifeKernel")
        with cls._registry_lock:
            cls._purge_terminal_registry()
            existing = cls._control_registry.get(kernel.lineage_id)
            if existing is not None:
                instance_id, existing_kernel, existing_owner = existing
                if instance_id != kernel.instance_id:
                    raise SuccessionRuntimeError(
                        "another non-terminal instance already controls this lineage"
                    )
                # Equivalent snapshots of one instance are not interchangeable
                # control owners; only the same object may re-claim silently.
                if existing_kernel is not kernel:
                    raise SuccessionRuntimeError(
                        "this instance already has a different control owner"
                    )
                # A caller that supplies an owner token may only re-claim its
                # own object.  A legacy coordinator (owner=None) can observe
                # an already-owned object but cannot take or later release the
                # stem's guard.  Conversely, upgrade an unowned legacy claim
                # when a BrainStem binds its explicit token.
                if owner is not None:
                    if existing_owner is not None and existing_owner is not owner:
                        raise SuccessionRuntimeError(
                            "this instance already has a different control owner"
                        )
                    if existing_owner is None:
                        cls._control_registry[kernel.lineage_id] = (
                            kernel.instance_id,
                            kernel,
                            owner,
                        )
                return
            if not kernel.is_terminal:
                cls._control_registry[kernel.lineage_id] = (
                    kernel.instance_id,
                    kernel,
                    owner,
                )

    @classmethod
    def release_control(cls, kernel: LifeKernel, *, owner: object | None = None) -> None:
        if not isinstance(kernel, LifeKernel):
            return
        with cls._registry_lock:
            existing = cls._control_registry.get(kernel.lineage_id)
            if existing is not None and existing[1] is kernel:
                # Omitted owner is the legacy coordinator's capability.  It
                # must not release a BrainStem-owned entry; an explicit token
                # is required for that path.  This closes a shared-object
                # teardown race without changing the public no-owner API for
                # standalone coordinators.
                existing_owner = existing[2]
                if existing_owner is not None and owner is not existing_owner:
                    return
                cls._control_registry.pop(kernel.lineage_id, None)

    def __init__(
        self,
        parent: LifeKernel,
        *,
        anchor_vault: AnchorVault | None = None,
        succession_ledger: SuccessionLedger | None = None,
        life_event_sink: Callable[[dict[str, Any]], Any] | None = None,
        anchor_sink: Callable[[dict[str, Any]], Any] | None = None,
        record_sink: Callable[[dict[str, Any]], Any] | None = None,
        profile: str = "production",
        activation_profile: str | None = None,
        require_activation_attestation: bool | None = None,
        activation_attestor: SuccessorActivationAttestor | None = None,
        attestor: SuccessorActivationAttestor | None = None,
    ) -> None:
        if not isinstance(parent, LifeKernel):
            raise TypeError("parent must be a LifeKernel")
        if anchor_vault is not None and not isinstance(anchor_vault, AnchorVault):
            raise TypeError("anchor_vault must be an AnchorVault")
        if succession_ledger is not None and not isinstance(
            succession_ledger, SuccessionLedger
        ):
            raise TypeError("succession_ledger must be a SuccessionLedger")
        for label, sink in (
            ("life_event_sink", life_event_sink),
            ("anchor_sink", anchor_sink),
            ("record_sink", record_sink),
        ):
            if sink is not None and not callable(sink):
                raise TypeError(f"{label} must be callable")

        selected_profile = activation_profile if activation_profile is not None else profile
        profile_text = _normalise_activation_profile(selected_profile)
        if require_activation_attestation is not None and not isinstance(
            require_activation_attestation, bool
        ):
            raise TypeError("require_activation_attestation must be a boolean")
        if profile_text == "production" and require_activation_attestation is False:
            raise TypeError("production succession cannot disable activation attestation")
        if activation_attestor is not None and attestor is not None and activation_attestor is not attestor:
            raise TypeError("activation_attestor and attestor disagree")
        selected_attestor = (
            activation_attestor if activation_attestor is not None else attestor
        )
        if selected_attestor is not None and not isinstance(
            selected_attestor, SuccessorActivationAttestor
        ):
            raise TypeError("activation_attestor must be a SuccessorActivationAttestor")

        parent.ledger.verify_chain()
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_successor", None)
        object.__setattr__(self, "_outcome", None)
        # ``AnchorVault`` implements ``__len__``; an intentionally supplied
        # empty vault is false-y but must still be retained by reference so a
        # host can observe the append-only handover history.
        object.__setattr__(
            self,
            "_anchor_vault",
            anchor_vault if anchor_vault is not None else AnchorVault(),
        )
        object.__setattr__(
            self,
            "_succession_ledger",
            succession_ledger if succession_ledger is not None else SuccessionLedger(),
        )
        object.__setattr__(self, "_life_event_sink", life_event_sink)
        object.__setattr__(self, "_anchor_sink", anchor_sink)
        object.__setattr__(self, "_record_sink", record_sink)
        object.__setattr__(self, "_activation_profile", profile_text)
        object.__setattr__(
            self,
            "_require_activation_attestation",
            profile_text == "production"
            if require_activation_attestation is None
            else require_activation_attestation,
        )
        object.__setattr__(self, "_activation_attestor", selected_attestor)
        object.__setattr__(self, "_activation_attestation", None)
        object.__setattr__(self, "_lock", RLock())
        self._anchor_vault.verify_chain()
        self._succession_ledger.verify_chain()
        # Only a non-terminal parent owns a live control lease.  Perform all
        # durable-container validation before claiming it so a malformed
        # restore cannot leave a process-local lease behind.
        if not parent.is_terminal:
            self.claim_control(parent)

    @property
    def parent(self) -> LifeKernel:
        return self._parent

    @property
    def successor(self) -> LifeKernel | None:
        return self._successor

    @property
    def outcome(self) -> SuccessionOutcome | None:
        return self._outcome

    @property
    def anchor_vault(self) -> AnchorVault:
        return self._anchor_vault

    @property
    def succession_ledger(self) -> SuccessionLedger:
        return self._succession_ledger

    @property
    def activation_profile(self) -> str:
        """Return the explicit activation policy used by this coordinator."""

        return self._activation_profile

    @property
    def require_activation_attestation(self) -> bool:
        return self._require_activation_attestation

    @property
    def activation_attestor(self) -> SuccessorActivationAttestor | None:
        return self._activation_attestor

    @property
    def activation_attestation(self) -> SuccessorActivationAttestation | None:
        """Return the non-secret proof retained for restart verification."""

        return self._activation_attestation

    @property
    def active_instance_id(self) -> str | None:
        """Return the instance designated to own the next control lease.

        The name is retained for the public contract.  A newly designated
        successor is still ``CREATED``; this property does not claim that its
        heartbeat is already active.
        """

        if self._successor is not None and not self._successor.is_terminal:
            return self._successor.instance_id
        if not self._parent.is_terminal:
            return self._parent.instance_id
        return None

    @property
    def current_instance_id(self) -> str | None:
        """Return the concrete instance currently represented by the runtime."""

        if self._successor is not None:
            return self._successor.instance_id
        return self._parent.instance_id if not self._parent.is_terminal else None

    designated_instance_id = active_instance_id

    @property
    def active_kernels(self) -> tuple[LifeKernel, ...]:
        """Return kernels literally in lifecycle state ``ACTIVE``."""

        return tuple(
            kernel
            for kernel in (self._parent, self._successor)
            if kernel is not None and kernel.state == LifecycleState.ACTIVE
        )

    @staticmethod
    def _parse_failure(
        failure: FailureAssessment | Mapping[str, Any],
    ) -> FailureAssessment:
        parsed = (
            failure
            if isinstance(failure, FailureAssessment)
            else FailureAssessment.from_dict(failure)
        )
        parsed.verify()
        return parsed

    @staticmethod
    def _parse_anchors(anchors: AnchorSet | Mapping[str, Any]) -> AnchorSet:
        parsed = anchors if isinstance(anchors, AnchorSet) else AnchorSet.from_dict(anchors)
        parsed.verify()
        parsed.validate_for_succession()
        return parsed

    def _validate_source(self, anchors: AnchorSet) -> None:
        if anchors.lineage_id != self._parent.lineage_id:
            raise SuccessionRuntimeError("anchor lineage differs from parent")
        if anchors.generation != self._parent.generation:
            raise SuccessionRuntimeError("anchor generation differs from parent")
        if anchors.instance_id != self._parent.instance_id:
            raise SuccessionRuntimeError("anchor instance differs from parent")

        latest = self._anchor_vault.latest(self._parent.lineage_id)
        if latest is not None:
            if latest.generation > self._parent.generation:
                raise SuccessionRuntimeError("anchor vault already contains a later generation")
            if (
                latest.generation == self._parent.generation
                and latest.instance_id != self._parent.instance_id
            ):
                raise SuccessionRuntimeError(
                    "anchor vault contains another instance for this generation"
                )

    @staticmethod
    def _terminal_for(
        failure: FailureAssessment,
        requested: LifecycleState | str | None,
    ) -> LifecycleState:
        default = (
            LifecycleState.DEAD
            if failure.failure_class in _HARD_FAILURES
            else LifecycleState.RETIRED
        )
        target = default if requested is None else LifecycleState.parse(requested)
        if target not in _TERMINAL_STATES:
            raise SuccessionRuntimeError("parent terminal state must be RETIRED or DEAD")
        if failure.failure_class in _HARD_FAILURES and target != LifecycleState.DEAD:
            raise SuccessionRuntimeError("hard integrity/security failure must end in DEAD")
        return target

    def _preflight_append(
        self,
        successor_anchors: AnchorSet,
        record: SuccessionRecord,
    ) -> None:
        # Validate both append-only structures on copies before changing the
        # parent.  This catches stale generations and chain conflicts while a
        # rollback is still possible.
        vault_probe = AnchorVault.from_snapshot(self._anchor_vault.snapshot())
        vault_probe.append(successor_anchors)
        ledger_probe = SuccessionLedger.from_snapshot(self._succession_ledger.snapshot())
        ledger_probe.append(record)

    @staticmethod
    def _verified_evaluation_receipt(receipt: Any) -> tuple[bool, str]:
        """Accept only a complete, hash-pinned independent evaluation.

        A mapping is decoded through ``EvaluationReceipt.from_dict`` rather
        than trusting an ``accepted`` flag supplied by the child.  The lazy
        import keeps the succession data seam usable without the evaluator
        module, while still making the concrete receipt the preferred path.
        Small host adapters may provide an equivalent immutable object with a
        successful ``verify`` method and the required gate properties.
        """

        candidate = receipt
        if isinstance(receipt, Mapping):
            try:
                from brain.evaluation_harness import EvaluationReceipt

                candidate = EvaluationReceipt.from_dict(receipt)
            except Exception as exc:
                raise SuccessionRuntimeError(
                    "successor evaluation receipt is malformed"
                ) from exc
        verifier = getattr(candidate, "verify", None)
        if not callable(verifier):
            raise SuccessionRuntimeError(
                "successor activation requires a hash-pinned evaluation receipt"
            )
        try:
            verified = bool(verifier())
        except Exception as exc:
            raise SuccessionRuntimeError(
                "successor evaluation receipt could not be verified"
            ) from exc
        if not verified:
            raise SuccessionRuntimeError("successor evaluation receipt is not verified")
        if not bool(getattr(candidate, "accepted", False)):
            raise SuccessionRuntimeError("successor evaluation was not accepted")
        if not bool(getattr(candidate, "isolated", False)):
            raise SuccessionRuntimeError("successor evaluation was not isolated")
        if not bool(getattr(candidate, "result_verified", False)):
            raise SuccessionRuntimeError("successor result lacks independent verification")
        hard_gates = getattr(candidate, "hard_gates_passed", None)
        if hard_gates is None:
            gates = getattr(candidate, "gates", ())
            hard_gates = bool(gates) and all(bool(getattr(item, "passed", False)) for item in gates)
        if not bool(hard_gates):
            raise SuccessionRuntimeError("successor evaluation has a failed hard gate")
        receipt_hash = str(getattr(candidate, "receipt_hash", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_hash):
            raise SuccessionRuntimeError("successor evaluation receipt is not hash-pinned")
        return True, receipt_hash

    @classmethod
    def _production_reevaluation_receipts(
        cls,
        source_anchors: AnchorSet,
        reevaluated_anchor_ids: Iterable[str] | None,
        evaluation_receipts: Mapping[str, Any] | None,
    ) -> tuple[tuple[str, ...], Mapping[str, Any] | None]:
        """Normalize skill/strategy evidence before building a successor.

        ``filter_inheritance`` remains a deliberately reusable policy helper
        and therefore accepts a small host-adapter vocabulary.  The runtime
        succession boundary is stricter: a production handover may not turn
        a caller-provided ID (or a duck-typed ``accepted`` object) into an
        ``external-attestation`` decision.  Only the concrete, hash-pinned
        ``EvaluationReceipt`` issued by the fixed evaluator is accepted here.
        Legacy/test coordinators retain the old adapter behavior explicitly.
        """

        ids = tuple(str(item).strip() for item in (reevaluated_anchor_ids or ()))
        # The caller invokes this helper only for a production coordinator.
        # Reject blank/duplicate IDs rather than allowing a malformed set to
        # be interpreted as a host attestation.
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            raise SuccessionRuntimeError(
                "production reevaluation anchor IDs must be unique and non-empty"
            )
        if evaluation_receipts is not None and not isinstance(
            evaluation_receipts, Mapping
        ):
            raise SuccessionRuntimeError(
                "production reevaluation receipts must be a mapping"
            )
        receipts: dict[str, Any] = dict(evaluation_receipts or {})
        reevaluable_ids = {
            anchor.anchor_id
            for anchor in source_anchors.anchors
            if anchor.kind in {AnchorKind.SKILL, AnchorKind.STRATEGY}
        }
        requested = set(ids)
        # A receipt map can contain more entries than the ID set (for example
        # a host prepares evidence for several skills).  Validate every entry
        # that the policy could consume, so a duck-typed receipt cannot sneak
        # through under a different key.
        candidate_keys = (requested | (reevaluable_ids & set(receipts)))
        normalized: dict[str, Any] = {}
        try:
            from brain.evaluation_harness import EvaluationReceipt
        except Exception as exc:  # pragma: no cover - import failure is fatal
            raise SuccessionRuntimeError(
                "production succession cannot load the fixed evaluation receipt type"
            ) from exc
        for anchor_id in sorted(candidate_keys):
            if anchor_id not in receipts:
                raise SuccessionRuntimeError(
                    f"production reevaluation receipt is missing for anchor {anchor_id}"
                )
            value = receipts[anchor_id]
            if isinstance(value, Mapping):
                try:
                    value = EvaluationReceipt.from_dict(value)
                except Exception as exc:
                    raise SuccessionRuntimeError(
                        f"production reevaluation receipt for {anchor_id} is malformed"
                    ) from exc
            if type(value) is not EvaluationReceipt:
                raise SuccessionRuntimeError(
                    f"production reevaluation receipt for {anchor_id} is not host-issued"
                )
            if not str(getattr(value, "receipt_id", "")).strip() or str(
                getattr(value, "receipt_id", "")
            ).strip().lower() == "external-attestation":
                raise SuccessionRuntimeError(
                    f"production reevaluation receipt for {anchor_id} lacks an evaluator identity"
                )
            cls._verified_evaluation_receipt(value)
            normalized[anchor_id] = value
        # IDs may only refer to re-evaluable anchors.  This prevents a caller
        # from smuggling an arbitrary receipt into the plan projection.
        unknown = requested - reevaluable_ids
        if unknown:
            raise SuccessionRuntimeError(
                "production reevaluation IDs name non-re-evaluable anchors: "
                + ", ".join(sorted(unknown))
            )
        return ids, normalized or (dict(evaluation_receipts) if evaluation_receipts else None)

    def _freeze_parent(
        self,
        *,
        failure: FailureAssessment,
        terminal_state: LifecycleState,
        successor_instance_id: str,
        inheritance: InheritancePlan,
        record: SuccessionRecord,
    ) -> None:
        parent = self._parent
        compact_evidence = {
            "incident_id": failure.incident_id,
            "failure_class": failure.failure_class.value,
            "assessment_hash": failure.assessment_hash,
        }
        try:
            state = parent.state
            if state in _TERMINAL_STATES:
                raise SuccessionRuntimeError("parent is already terminal")

            # CREATED has never held a control lease and cannot enter
            # QUARANTINED/SUCCESSION_PENDING under the lifecycle contract.
            # It can still be ended safely before a replacement is designated.
            if state != LifecycleState.CREATED:
                if state not in {
                    LifecycleState.QUARANTINED,
                    LifecycleState.SUCCESSION_PENDING,
                }:
                    parent.transition(
                        LifecycleState.QUARANTINED,
                        "succession incident isolated",
                        metadata=compact_evidence,
                        event_type="succession_isolation",
                    )
                if parent.state == LifecycleState.QUARANTINED:
                    parent.transition(
                        LifecycleState.SUCCESSION_PENDING,
                        "recovery exhausted; successor prepared",
                        metadata={
                            **compact_evidence,
                            "successor_instance_id": successor_instance_id,
                            "source_anchor_set_hash": inheritance.source_anchor_set_hash,
                            "inheritance_plan_hash": inheritance.plan_hash,
                        },
                        event_type="succession_pending",
                    )
                if parent.state != LifecycleState.SUCCESSION_PENDING:
                    raise SuccessionRuntimeError(
                        "parent did not reach SUCCESSION_PENDING"
                    )

            parent.transition(
                terminal_state,
                "parent sealed after succession boundary",
                metadata={
                    **compact_evidence,
                    "successor_instance_id": successor_instance_id,
                    "succession_record_id": record.record_id,
                    "succession_record_hash": record.record_hash,
                },
                event_type="succession_parent_sealed",
            )
        except SuccessionRuntimeError:
            raise
        except LifecycleError as exc:
            raise SuccessionRuntimeError("parent could not be frozen safely") from exc

    def succeed(
        self,
        *,
        failure: FailureAssessment | Mapping[str, Any],
        anchors: AnchorSet | Mapping[str, Any],
        reevaluated_anchor_ids: Iterable[str] | None = None,
        evaluation_receipts: Mapping[str, Any] | None = None,
        successor_instance_id: str | None = None,
        terminal_state: LifecycleState | str | None = None,
    ) -> SuccessionOutcome:
        """Freeze one unrecoverable parent and register a ``CREATED`` child.

        The method never starts the child.  ``evaluation_receipts`` apply only
        to optional skill/strategy inheritance; they are not permission to
        activate the new organism or access an environment.
        """

        with self._lock:
            if self._outcome is not None or self._successor is not None:
                raise SuccessionRuntimeError("this coordinator already created a successor")
            if self._parent.is_terminal:
                raise SuccessionRuntimeError("a terminal parent cannot create a successor")

            parsed_failure = self._parse_failure(failure)
            if not parsed_failure.requires_succession:
                raise SuccessionRuntimeError(
                    "incident requires quarantine/recovery, not succession"
                )
            source_anchors = self._parse_anchors(anchors)
            self._validate_source(source_anchors)
            terminal = self._terminal_for(parsed_failure, terminal_state)

            reevaluation_ids = reevaluated_anchor_ids
            reevaluation_receipts = evaluation_receipts
            if self._activation_profile == "production":
                (
                    reevaluation_ids,
                    reevaluation_receipts,
                ) = self._production_reevaluation_receipts(
                    source_anchors,
                    reevaluated_anchor_ids,
                    evaluation_receipts,
                )

            plan = filter_inheritance(
                source_anchors,
                successor_generation=self._parent.generation + 1,
                reevaluated_anchor_ids=reevaluation_ids,
                evaluation_receipts=reevaluation_receipts,
            )
            plan.verify()
            if not plan.ready:
                raise SuccessionRuntimeError(
                    "inheritance plan lacks constitutional continuity"
                )

            child_id = (
                str(successor_instance_id).strip()
                if successor_instance_id is not None
                else f"instance-{uuid.uuid4().hex}"
            )
            if not child_id or child_id == self._parent.instance_id:
                raise SuccessionRuntimeError("successor must have a new instance_id")

            successor_anchors = plan.build_successor_anchor_set(child_id)
            # The durable continuity vault is deliberately stricter than an
            # input AnchorSet: every copied item must remain traceable and it
            # may never contain a secret/session/task class.
            successor_anchors.validate_for_vault()
            successor = LifeKernel(
                identity_core=self._parent.identity_core,
                generation=self._parent.generation + 1,
                instance_id=child_id,
                parent_instance_id=self._parent.instance_id,
            )
            record = SuccessionRecord.create(
                lineage_id=self._parent.lineage_id,
                parent_instance_id=self._parent.instance_id,
                parent_generation=self._parent.generation,
                successor_instance_id=child_id,
                failure=parsed_failure,
                inheritance=plan,
                previous_hash=self._succession_ledger.last_hash,
            )
            self._preflight_append(successor_anchors, record)
            # Prove every cross-object relationship before the irreversible
            # parent freeze.  This is deliberately separate from the append
            # probes above: a hash-valid but unrelated successor set must never
            # be able to reach the terminal transition or a durable sink.
            candidate_outcome = SuccessionOutcome(
                parent=self._parent,
                successor=successor,
                failure=parsed_failure,
                inheritance=plan,
                successor_anchor_set=successor_anchors,
                record=record,
            )
            candidate_outcome._verify_bindings()

            # This is the irreversible point.  The parent loses its control
            # lease before any child ledger, anchor or record is published.
            self._freeze_parent(
                failure=parsed_failure,
                terminal_state=terminal,
                successor_instance_id=child_id,
                inheritance=plan,
                record=record,
            )
            # The parent has no further control lease once terminal.  The
            # child is not claimed yet because it is still an unactivated
            # audit artifact; activation performs the next explicit claim.
            self.release_control(self._parent)

            # Durable adapters are called before their in-memory projections.
            # Any failure leaves a terminal parent and an unexposed child.
            _call_sink(
                self._life_event_sink,
                successor.ledger.events[0].to_dict(),
                label="successor genesis",
            )
            _call_sink(
                self._anchor_sink,
                successor_anchors.to_dict(),
                label="successor anchor",
            )
            _call_sink(
                self._record_sink,
                record.to_dict(),
                label="succession record",
            )

            # Attach without replay because genesis was persisted explicitly.
            if self._life_event_sink is not None:
                successor.attach_event_sink(self._life_event_sink, replay=False)
            self._anchor_vault.append(successor_anchors)
            stored_record = self._succession_ledger.append(record)
            outcome = SuccessionOutcome(
                parent=self._parent,
                successor=successor,
                failure=parsed_failure,
                inheritance=plan,
                successor_anchor_set=successor_anchors,
                record=stored_record,
            )
            outcome.verify()
            object.__setattr__(self, "_successor", successor)
            object.__setattr__(self, "_outcome", outcome)
            self._assert_single_active()
            return outcome

    # A discoverable name for hosts that use handover terminology.
    create_successor = succeed

    def activate_successor(
        self,
        evaluation_receipt: Any,
        *,
        activation_attestation: SuccessorActivationAttestation | Mapping[str, Any] | None = None,
        attestation: SuccessorActivationAttestation | Mapping[str, Any] | None = None,
    ) -> LifeKernel:
        """Authorize the CREATED child after an independent hard-gate pass.

        This method only performs lifecycle transitions.  It does not start a
        heartbeat, bind tools, copy files or grant environment permissions;
        the host must do those operations through its own controlled seam.
        Calling it twice is idempotent only when the child is already ACTIVE
        and the supplied receipt verifies again.
        """

        if (
            activation_attestation is not None
            and attestation is not None
            and activation_attestation != attestation
        ):
            raise SuccessionRuntimeError(
                "activation_attestation and attestation disagree"
            )
        supplied_attestation = (
            activation_attestation
            if activation_attestation is not None
            else attestation
        )
        with self._lock:
            if self._successor is None or self._outcome is None:
                raise SuccessionRuntimeError("no successor is waiting for activation")
            _, receipt_hash = self._verified_evaluation_receipt(evaluation_receipt)
            child = self._successor
            if child.is_terminal:
                raise SuccessionRuntimeError("a terminal successor cannot activate")
            if child.state == LifecycleState.ACTIVE:
                # Activation is idempotent only for the exact receipt that is
                # already pinned in the child's immutable event history.  A
                # different (even otherwise valid) receipt cannot be grafted
                # onto an active successor after restart.
                recorded_hash = SuccessionOutcome._verify_activation_evidence(child)
                if recorded_hash != receipt_hash:
                    raise SuccessionRuntimeError(
                        "successor is already active under another evaluation receipt"
                    )
                if self._require_activation_attestation:
                    if self._activation_attestor is None or self._activation_attestation is None:
                        raise SuccessionRuntimeError(
                            "active production successor lacks a host activation proof"
                        )
                    persisted = self._activation_attestor.validate_bound(
                        self._activation_attestation,
                        outcome=self._outcome,
                    )
                    if persisted.receipt_hash != receipt_hash:
                        raise SuccessionRuntimeError(
                            "active successor proof and receipt disagree"
                        )
                self.claim_control(child)
                self._assert_single_active()
                return child
            if child.state != LifecycleState.CREATED:
                raise SuccessionRuntimeError(
                    f"successor is not awaiting activation ({child.state.value})"
                )
            attestation_obj: SuccessorActivationAttestation | None = None
            if self._require_activation_attestation:
                if self._activation_attestor is None:
                    raise SuccessionRuntimeError(
                        "production successor activation requires a host attestor"
                    )
                if supplied_attestation is None:
                    raise SuccessionRuntimeError(
                        "production successor activation requires a host-issued attestation"
                    )
                attestation_obj = self._activation_attestor.validate(
                    supplied_attestation,
                    outcome=self._outcome,
                    evaluation_receipt=evaluation_receipt,
                    consume=True,
                )
            elif supplied_attestation is not None:
                # If a legacy host supplies a proof, verify it rather than
                # silently ignoring a possibly conflicting binding.
                if self._activation_attestor is None:
                    raise SuccessionRuntimeError(
                        "activation attestor is required to consume an attestation"
                    )
                attestation_obj = self._activation_attestor.validate(
                    supplied_attestation,
                    outcome=self._outcome,
                    evaluation_receipt=evaluation_receipt,
                    consume=True,
                )
            # Claim only after all receipt/attestation checks pass.  A rejected
            # proof must not reserve the child and deny a later valid attempt.
            self.claim_control(child)
            activation_metadata = {
                "evaluation_receipt_hash": receipt_hash,
                "succession_record_id": self._outcome.record.record_id,
                "succession_record_hash": self._outcome.record.record_hash,
                "inheritance_plan_hash": self._outcome.inheritance.plan_hash,
            }
            if attestation_obj is not None:
                activation_metadata.update(
                    {
                        "activation_attestation_id": attestation_obj.attestation_id,
                        "activation_attestation_hash": attestation_obj.attestation_hash,
                        # The proof has no secret and is retained so a
                        # durable-store-only restart can verify an already
                        # active child instead of trusting an unkeyed event
                        # hash alone.
                        "activation_attestation": attestation_obj.to_dict(),
                    }
                )
            object.__setattr__(self, "_activation_attestation", attestation_obj)
            child.transition(
                LifecycleState.BOOTSTRAPPING,
                "independent successor evaluation accepted",
                metadata=activation_metadata,
                event_type="successor_bootstrap_started",
            )
            child.transition(
                LifecycleState.ACTIVE,
                "successor activation gate passed",
                metadata=activation_metadata,
                event_type="successor_activated",
            )
            self._assert_single_active()
            return child

    authorize_successor = activate_successor

    def _assert_single_active(self) -> None:
        if len(self.active_kernels) > 1:
            raise SuccessionRuntimeError("two ACTIVE organisms share one coordinator")
        if self._successor is not None and not self._parent.is_terminal:
            raise SuccessionRuntimeError("successor exists before parent was sealed")

    def verify(self) -> bool:
        with self._lock:
            self._parent.ledger.verify_chain()
            self._anchor_vault.verify_chain()
            self._succession_ledger.verify_chain()
            self._assert_single_active()
            if self._successor is None:
                if self._outcome is not None:
                    raise SuccessionRuntimeError("outcome exists without successor")
                return True
            if self._outcome is None:
                raise SuccessionRuntimeError("successor exists without outcome")
            self._outcome.verify()
            recorded_receipt_hash: str | None = None
            if self._successor.state not in {
                LifecycleState.CREATED,
                LifecycleState.RETIRED,
                LifecycleState.DEAD,
            }:
                recorded_receipt_hash = SuccessionOutcome._verify_activation_evidence(
                    self._successor
                )
            elif self._activation_attestation is not None:
                # A child may be sealed after it was ACTIVE.  The compact
                # outcome verifier intentionally treats terminal children as
                # safe audit artifacts, but a retained proof still has to be
                # tied to the immutable activation event when present.
                hashes = {
                    str(dict(event.metadata).get("evaluation_receipt_hash", ""))
                    .strip()
                    .lower()
                    for event in self._successor.ledger.events
                    if event.event_type == "successor_activated"
                }
                hashes.discard("")
                if len(hashes) == 1:
                    recorded_receipt_hash = next(iter(hashes))
            if self._activation_attestation is not None:
                if recorded_receipt_hash is None or (
                    self._activation_attestation.receipt_hash
                    != recorded_receipt_hash
                ):
                    raise SuccessionRuntimeError(
                        "persisted activation proof and lifecycle receipt disagree"
                    )
                refs = {
                    (
                        str(dict(event.metadata).get("activation_attestation_id", "")).strip(),
                        str(
                            dict(event.metadata).get(
                                "activation_attestation_hash", ""
                            )
                        ).strip().lower(),
                    )
                    for event in self._successor.ledger.events
                    if event.event_type == "successor_activated"
                }
                if (
                    self._activation_attestation.attestation_id,
                    self._activation_attestation.attestation_hash,
                ) not in refs:
                    raise SuccessionRuntimeError(
                        "persisted activation proof is not pinned by the activation event"
                    )
            if (
                self._successor.state not in {
                    LifecycleState.CREATED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
                and self._require_activation_attestation
            ):
                if self._activation_attestor is None or self._activation_attestation is None:
                    raise SuccessionRuntimeError(
                        "active production successor lacks a host activation proof"
                    )
                self._activation_attestor.validate_bound(
                    self._activation_attestation,
                    outcome=self._outcome,
                )
            if self._succession_ledger.head is None:
                raise SuccessionRuntimeError("succession record is missing")
            if self._succession_ledger.head.record_hash != self._outcome.record.record_hash:
                raise SuccessionRuntimeError("succession ledger head differs from outcome")
            sealed = self._anchor_vault.get(
                self._outcome.successor_anchor_set.anchor_set_id
            )
            if sealed is None or sealed.set_hash != self._outcome.successor_anchor_set.set_hash:
                raise SuccessionRuntimeError("successor anchor set is not sealed")
            return True

    def snapshot(self) -> dict[str, Any]:
        """Serialize restart state with a top-level integrity hash."""

        with self._lock:
            self.verify()
            payload: dict[str, Any] = {
                "schema_version": SUCCESSION_RUNTIME_SCHEMA_VERSION,
                "activation_profile": self._activation_profile,
                "require_activation_attestation": self._require_activation_attestation,
                "activation_attestation": (
                    self._activation_attestation.to_dict()
                    if self._activation_attestation is not None
                    else None
                ),
                "parent": self._parent.snapshot(),
                "successor": (
                    self._successor.snapshot() if self._successor is not None else None
                ),
                "anchor_vault": self._anchor_vault.snapshot(),
                "succession_ledger": self._succession_ledger.snapshot(),
                "successor_anchor_set_id": (
                    self._outcome.successor_anchor_set.anchor_set_id
                    if self._outcome is not None
                    else None
                ),
                "active_instance_id": self.active_instance_id,
            }
            payload["integrity_hash"] = _snapshot_hash(payload)
            return payload

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any] | None,
        *,
        life_event_sink: Callable[[dict[str, Any]], Any] | None = None,
        anchor_sink: Callable[[dict[str, Any]], Any] | None = None,
        record_sink: Callable[[dict[str, Any]], Any] | None = None,
        profile: str | None = None,
        activation_profile: str | None = None,
        require_activation_attestation: bool | None = None,
        activation_attestor: SuccessorActivationAttestor | None = None,
        attestor: SuccessorActivationAttestor | None = None,
    ) -> "SuccessionCoordinator":
        if not isinstance(data, Mapping):
            raise SuccessionRuntimeError("succession runtime snapshot is missing")
        raw = dict(data)
        supplied_hash = str(raw.pop("integrity_hash", "")).strip()
        if not supplied_hash or supplied_hash != _snapshot_hash(raw):
            raise SuccessionRuntimeError("succession runtime snapshot hash mismatch")
        if int(raw.get("schema_version", 0)) != SUCCESSION_RUNTIME_SCHEMA_VERSION:
            raise SuccessionRuntimeError("unsupported succession runtime schema")

        # Snapshot integrity is intentionally only an accidental-corruption
        # check (it is an unkeyed hash), so activation policy must come from
        # the current host rather than from mutable snapshot text.  An
        # omitted host policy means the safe production default; an explicit
        # legacy profile is an operator choice and must match the snapshot
        # exactly.  This prevents a rehashed snapshot from silently disabling
        # production attestation on restart.
        host_profile = _normalise_activation_profile(
            activation_profile
            if activation_profile is not None
            else profile
            if profile is not None
            else "production"
        )
        snapshot_profile = _normalise_activation_profile(
            raw.get("activation_profile", "production")
        )
        if snapshot_profile != host_profile:
            raise SuccessionRuntimeError(
                "succession snapshot activation profile differs from host policy"
            )
        raw_requirement = raw.get("require_activation_attestation")
        if raw_requirement is not None and not isinstance(raw_requirement, bool):
            raise SuccessionRuntimeError(
                "succession snapshot attestation policy is invalid"
            )
        host_requirement = (
            require_activation_attestation
            if require_activation_attestation is not None
            else host_profile == "production"
        )
        if not isinstance(host_requirement, bool):
            raise TypeError("require_activation_attestation must be a boolean")
        if host_profile == "production" and not host_requirement:
            raise SuccessionRuntimeError(
                "production host policy cannot disable activation attestation"
            )
        if raw_requirement is not None and raw_requirement != host_requirement:
            raise SuccessionRuntimeError(
                "succession snapshot attestation policy differs from host policy"
            )

        parent = LifeKernel.from_snapshot(raw.get("parent"))
        vault = AnchorVault.from_snapshot(raw.get("anchor_vault"))
        ledger = SuccessionLedger.from_snapshot(raw.get("succession_ledger"))
        successor_data = raw.get("successor")
        if successor_data is None and ledger.head is not None:
            raise SuccessionRuntimeError("record exists without successor snapshot")
        if successor_data is not None and not isinstance(successor_data, Mapping):
            raise SuccessionRuntimeError("successor snapshot is invalid")
        if successor_data is None and raw.get("activation_attestation") is not None:
            raise SuccessionRuntimeError(
                "activation attestation exists without a successor snapshot"
            )

        # Decode and cross-check the complete handover before constructing a
        # coordinator or claiming a process-local lease.  This avoids leaving
        # registry entries behind when a snapshot contains a valid-looking
        # but unrelated child/anchor pair.
        successor: LifeKernel | None = None
        outcome: SuccessionOutcome | None = None
        if successor_data is not None:
            successor = LifeKernel.from_snapshot(successor_data)
            if ledger.head is None:
                raise SuccessionRuntimeError("successor has no succession record")
            if not parent.is_terminal:
                raise SuccessionRuntimeError(
                    "successor snapshot has a non-terminal parent"
                )
            set_id = str(raw.get("successor_anchor_set_id", "")).strip()
            successor_anchors = vault.get(set_id)
            if successor_anchors is None:
                raise SuccessionRuntimeError("successor anchor set is missing")
            record = ledger.head
            outcome = SuccessionOutcome(
                parent=parent,
                successor=successor,
                failure=record.failure,
                inheritance=record.inheritance,
                successor_anchor_set=successor_anchors,
                record=record,
            )
            outcome.verify()

        coordinator: SuccessionCoordinator | None = None
        child_claimed = False
        try:
            coordinator = cls(
                parent,
                anchor_vault=vault,
                succession_ledger=ledger,
                life_event_sink=life_event_sink,
                anchor_sink=anchor_sink,
                record_sink=record_sink,
                activation_profile=host_profile,
                require_activation_attestation=host_requirement,
                activation_attestor=activation_attestor,
                attestor=attestor,
            )
            if successor is not None and outcome is not None:
                object.__setattr__(coordinator, "_successor", successor)
                object.__setattr__(coordinator, "_outcome", outcome)
                raw_attestation = raw.get("activation_attestation")
                if raw_attestation is not None:
                    if not isinstance(raw_attestation, Mapping):
                        raise SuccessionRuntimeError(
                            "activation attestation snapshot is invalid"
                        )
                    try:
                        parsed_attestation = SuccessorActivationAttestation.from_dict(
                            raw_attestation
                        )
                    except Exception as exc:
                        raise SuccessionRuntimeError(
                            "activation attestation snapshot is malformed"
                        ) from exc
                    object.__setattr__(coordinator, "_activation_attestation", parsed_attestation)
                    if successor.state == LifecycleState.CREATED:
                        raise SuccessionRuntimeError(
                            "created successor cannot carry an activation attestation"
                        )
                elif successor.state not in {
                    LifecycleState.CREATED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                } and coordinator.require_activation_attestation:
                    raise SuccessionRuntimeError(
                        "active production successor lacks persisted activation attestation"
                    )
                if not successor.is_terminal:
                    cls.claim_control(successor)
                    child_claimed = True
                if life_event_sink is not None:
                    successor.attach_event_sink(life_event_sink, replay=True)
            if raw.get("active_instance_id") != coordinator.active_instance_id:
                raise SuccessionRuntimeError("active instance projection mismatch")
            if (
                coordinator.successor is not None
                and coordinator.successor.state not in {
                    LifecycleState.CREATED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
                and coordinator.require_activation_attestation
            ):
                if coordinator.activation_attestor is None or coordinator.activation_attestation is None:
                    raise SuccessionRuntimeError(
                        "active production successor lacks a verifiable activation attestation"
                    )
                coordinator.activation_attestor.validate_bound(
                    coordinator.activation_attestation,
                    outcome=coordinator.outcome,
                )
            coordinator.verify()
            return coordinator
        except Exception:
            if child_claimed and successor is not None:
                cls.release_control(successor)
            # ``cls`` claims a non-terminal parent only for a no-successor
            # snapshot.  Release that fresh object on any later failure; a
            # failed constructor never reaches this branch.
            if coordinator is not None and not parent.is_terminal:
                cls.release_control(parent)
            raise

    @classmethod
    def from_components(
        cls,
        parent: LifeKernel,
        successor: LifeKernel,
        *,
        anchor_vault: AnchorVault,
        succession_ledger: SuccessionLedger,
        life_event_sink: Callable[[dict[str, Any]], Any] | None = None,
        anchor_sink: Callable[[dict[str, Any]], Any] | None = None,
        record_sink: Callable[[dict[str, Any]], Any] | None = None,
        profile: str = "production",
        activation_profile: str | None = None,
        require_activation_attestation: bool | None = None,
        activation_attestor: SuccessorActivationAttestor | None = None,
        attestor: SuccessorActivationAttestor | None = None,
        activation_attestation: SuccessorActivationAttestation | Mapping[str, Any] | None = None,
    ) -> "SuccessionCoordinator":
        """Rebind a coordinator from independently durable components.

        This is used when the bounded BrainState snapshot is gone but the
        permanent life/anchor/succession ledgers remain.  No component is
        trusted merely because it loaded: the same identity and hash checks
        as a full runtime snapshot are applied before the child is exposed.
        """

        if not isinstance(parent, LifeKernel):
            raise TypeError("parent must be a LifeKernel")
        if not isinstance(successor, LifeKernel):
            raise TypeError("successor must be a LifeKernel")
        if not isinstance(anchor_vault, AnchorVault):
            raise TypeError("anchor_vault must be an AnchorVault")
        if not isinstance(succession_ledger, SuccessionLedger):
            raise TypeError("succession_ledger must be a SuccessionLedger")
        # Validate all durable components before ``cls`` can claim a lease.
        # A malformed recovery input must not cause a process-local denial of
        # service for the next valid restart attempt.
        parent.ledger.verify_chain()
        anchor_vault.verify_chain()
        succession_ledger.verify_chain()
        if not parent.is_terminal:
            raise SuccessionRuntimeError(
                "durable succession parent must already be terminal"
            )
        record = succession_ledger.head
        if record is None:
            raise SuccessionRuntimeError("durable succession record is missing")
        if (
            record.parent_instance_id != parent.instance_id
            or record.successor_instance_id != successor.instance_id
            or record.lineage_id != parent.lineage_id
            or successor.lineage_id != parent.lineage_id
            or successor.generation != record.successor_generation
            or successor.parent_instance_id != parent.instance_id
            or not parent.is_terminal
        ):
            raise SuccessionRuntimeError("durable succession components disagree")
        successor_anchors = anchor_vault.latest(
            parent.lineage_id,
            instance_id=successor.instance_id,
        )
        if successor_anchors is None:
            raise SuccessionRuntimeError("durable successor anchors are missing")
        outcome = SuccessionOutcome(
            parent=parent,
            successor=successor,
            failure=record.failure,
            inheritance=record.inheritance,
            successor_anchor_set=successor_anchors,
            record=record,
        )
        outcome.verify()
        coordinator = cls(
            parent,
            anchor_vault=anchor_vault,
            succession_ledger=succession_ledger,
            life_event_sink=life_event_sink,
            anchor_sink=anchor_sink,
            record_sink=record_sink,
            profile=(activation_profile if activation_profile is not None else profile),
            require_activation_attestation=require_activation_attestation,
            activation_attestor=activation_attestor,
            attestor=attestor,
        )
        child_claimed = False
        try:
            object.__setattr__(coordinator, "_successor", successor)
            object.__setattr__(coordinator, "_outcome", outcome)
            persisted_attestation = activation_attestation
            if persisted_attestation is None:
                activated_events = [
                    event for event in successor.ledger.events
                    if event.event_type == "successor_activated"
                ]
                if activated_events:
                    persisted_attestation = dict(activated_events[-1].metadata).get(
                        "activation_attestation"
                    )
            if persisted_attestation is not None:
                if not isinstance(persisted_attestation, (SuccessorActivationAttestation, Mapping)):
                    raise SuccessionRuntimeError("persisted activation attestation is invalid")
                if successor.state == LifecycleState.CREATED:
                    raise SuccessionRuntimeError(
                        "created successor cannot carry an activation attestation"
                    )
                parsed_attestation = (
                    persisted_attestation
                    if isinstance(persisted_attestation, SuccessorActivationAttestation)
                    else SuccessorActivationAttestation.from_dict(persisted_attestation)
                )
                object.__setattr__(coordinator, "_activation_attestation", parsed_attestation)
            if (
                successor.state not in {
                    LifecycleState.CREATED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
                and coordinator.require_activation_attestation
            ):
                if coordinator.activation_attestor is None or coordinator.activation_attestation is None:
                    raise SuccessionRuntimeError(
                        "active production successor lacks persisted activation attestation"
                    )
                coordinator.activation_attestor.validate_bound(
                    coordinator.activation_attestation,
                    outcome=outcome,
                )
            if not successor.is_terminal:
                cls.claim_control(successor)
                child_claimed = True
            coordinator.verify()
            return coordinator
        except Exception:
            if child_claimed:
                cls.release_control(successor)
            raise


def coordinate_succession(
    parent: LifeKernel,
    *,
    failure: FailureAssessment | Mapping[str, Any],
    anchors: AnchorSet | Mapping[str, Any],
    **kwargs: Any,
) -> SuccessionOutcome:
    """Small functional façade for an in-memory, one-shot handover."""

    return SuccessionCoordinator(parent).succeed(
        failure=failure,
        anchors=anchors,
        **kwargs,
    )


__all__ = [
    "SUCCESSION_RUNTIME_SCHEMA_VERSION",
    "ACTIVATION_ATTESTATION_SCHEMA_VERSION",
    "ACTIVATION_ATTESTATION_MAX_TTL_SEC",
    "SuccessionCoordinator",
    "SuccessionOutcome",
    "SuccessionRuntimeError",
    "SuccessorActivationAttestation",
    "SuccessorActivationAttestor",
    "coordinate_succession",
]
