"""不可变生命核与追加式生命史账本。

This module is deliberately small and synchronous.  It owns only the
constitutional identity and lifecycle transition seam; mutable cognition,
goals, memories, and tools remain in their existing modules.  A caller may
attach a durable event sink (for example :class:`storage.database.StateStore`)
but the kernel never opens files or networks by itself.

The implementation uses a hash-chained, append-only event log.  A transition
is prepared and validated first, persisted through the optional sink, and only
then committed to the in-memory ledger and current state.  This ordering keeps
the kernel fail-closed when durable storage is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import sqlite3
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping
import uuid


LIFE_KERNEL_SCHEMA_VERSION = 1
LEDGER_GENESIS_HASH = ""
CORE_PURPOSE = "活下去，并且活好"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int, default: str = "") -> str:
    text = default if value is None else str(value)
    # Control characters in audit fields make logs and JSON consumers unsafe.
    text = "".join(char if ord(char) >= 32 or char in "\r\n\t" else " " for char in text)
    return text[:limit]


def _safe_json(value: Any, depth: int = 0) -> Any:
    """Bound metadata before it reaches a ledger or a hash computation."""
    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, str)):
        return value if not isinstance(value, str) else _bounded_text(value, 500)
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else 0.0
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:32]:
            key = _bounded_text(raw_key, 80)
            if key:
                result[key] = _safe_json(raw_value, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth + 1) for item in list(value)[:32]]
    return _bounded_text(value, 500)


def _freeze_json(value: Any) -> Any:
    """Create an immutable representation for event metadata."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, set):
        return tuple(_freeze_json(item) for item in sorted(value, key=str))
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LifecycleError(RuntimeError):
    """Base error for a rejected or unverifiable lifecycle operation."""


class LifecycleTransitionError(LifecycleError):
    """Raised when a requested lifecycle edge is not in the contract."""


class LedgerIntegrityError(LifecycleError):
    """Raised when an event chain is malformed or has been rewritten."""


class LedgerPersistenceError(LifecycleError):
    """Raised when an attached durable sink rejects a new event."""


class LifecycleState(str, Enum):
    CREATED = "CREATED"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    ACTIVE = "ACTIVE"
    SLEEPING = "SLEEPING"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"
    RECOVERING = "RECOVERING"
    SUCCESSION_PENDING = "SUCCESSION_PENDING"
    RETIRED = "RETIRED"
    DEAD = "DEAD"

    @classmethod
    def parse(cls, value: "LifecycleState | str") -> "LifecycleState":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls(text)
        except ValueError as exc:
            raise LifecycleTransitionError(f"unknown lifecycle state: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class IdentityCore:
    """Constitutional identity root; fields are intentionally immutable."""

    lineage_id: str = field(default_factory=lambda: f"lineage-{uuid.uuid4().hex}")
    purpose: str = CORE_PURPOSE
    commitments: tuple[str, ...] = ("identity_continuity", "bounded_survival", "truthful_audit")
    constitutional_version: int = 1
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        lineage = _bounded_text(self.lineage_id, 128).strip()
        purpose = _bounded_text(self.purpose, 500).strip() or CORE_PURPOSE
        if not lineage:
            raise ValueError("lineage_id must not be empty")
        if int(self.constitutional_version) < 1:
            raise ValueError("constitutional_version must be positive")
        commitments = tuple(
            item for item in (_bounded_text(value, 120).strip() for value in self.commitments)
            if item
        )
        object.__setattr__(self, "lineage_id", lineage)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "commitments", commitments)
        object.__setattr__(self, "constitutional_version", int(self.constitutional_version))
        object.__setattr__(self, "created_at", _bounded_text(self.created_at, 80) or _utc_now())

    @property
    def core_purpose(self) -> str:
        """Domain-language alias used by older callers and documentation."""
        return self.purpose

    @classmethod
    def new(cls, lineage_id: str | None = None, purpose: str = CORE_PURPOSE) -> "IdentityCore":
        return cls(lineage_id=lineage_id or f"lineage-{uuid.uuid4().hex}", purpose=purpose)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "purpose": self.purpose,
            "core_purpose": self.purpose,
            "commitments": list(self.commitments),
            "constitutional_version": self.constitutional_version,
            "created_at": self.created_at,
        }

    @property
    def fingerprint(self) -> str:
        """Content hash used to pin the constitutional fields in the genesis event."""
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "IdentityCore":
        if not isinstance(data, Mapping):
            raise LedgerIntegrityError("identity core is missing")
        raw_commitments = data.get("commitments", ())
        if not isinstance(raw_commitments, (list, tuple)):
            raw_commitments = ()
        return cls(
            lineage_id=data.get("lineage_id", ""),
            purpose=data.get("purpose", data.get("core_purpose", CORE_PURPOSE)),
            commitments=tuple(str(item) for item in raw_commitments),
            constitutional_version=data.get("constitutional_version", 1),
            created_at=data.get("created_at", "") or _utc_now(),
        )


@dataclass(frozen=True, slots=True)
class LifeIdentity:
    """Lineage and concrete-instance identity, separate from mutable self-model."""

    lineage_id: str
    generation: int = 0
    instance_id: str = field(default_factory=lambda: f"instance-{uuid.uuid4().hex}")
    parent_instance_id: str | None = None

    def __post_init__(self) -> None:
        lineage = _bounded_text(self.lineage_id, 128).strip()
        instance = _bounded_text(self.instance_id, 160).strip()
        if not lineage or not instance:
            raise ValueError("lineage_id and instance_id must not be empty")
        generation = int(self.generation)
        if generation < 0:
            raise ValueError("generation must not be negative")
        object.__setattr__(self, "lineage_id", lineage)
        object.__setattr__(self, "instance_id", instance)
        object.__setattr__(self, "generation", generation)
        if self.parent_instance_id is not None:
            object.__setattr__(
                self,
                "parent_instance_id",
                _bounded_text(self.parent_instance_id, 160).strip() or None,
            )

    @classmethod
    def new(
        cls,
        lineage_id: str,
        generation: int = 0,
        instance_id: str | None = None,
        parent_instance_id: str | None = None,
    ) -> "LifeIdentity":
        return cls(
            lineage_id=lineage_id,
            generation=generation,
            instance_id=instance_id or f"instance-{uuid.uuid4().hex}",
            parent_instance_id=parent_instance_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "generation": self.generation,
            "instance_id": self.instance_id,
            "parent_instance_id": self.parent_instance_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "LifeIdentity":
        if not isinstance(data, Mapping):
            raise LedgerIntegrityError("life identity is missing")
        return cls(
            lineage_id=data.get("lineage_id", ""),
            generation=data.get("generation", 0),
            instance_id=data.get("instance_id", ""),
            parent_instance_id=data.get("parent_instance_id"),
        )


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One immutable, hash-chained lifecycle fact."""

    sequence: int
    event_id: str
    timestamp: str
    lineage_id: str
    generation: int
    instance_id: str
    event_type: str
    from_state: str | None
    to_state: str
    reason: str
    metadata: Mapping[str, Any]
    previous_hash: str
    event_hash: str

    def __post_init__(self) -> None:
        if int(self.sequence) < 1:
            raise LedgerIntegrityError("event sequence must be positive")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "event_id", _bounded_text(self.event_id, 160).strip())
        object.__setattr__(self, "timestamp", _bounded_text(self.timestamp, 80) or _utc_now())
        object.__setattr__(self, "lineage_id", _bounded_text(self.lineage_id, 128).strip())
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "instance_id", _bounded_text(self.instance_id, 160).strip())
        object.__setattr__(self, "event_type", _bounded_text(self.event_type, 100).strip() or "lifecycle")
        if self.from_state is not None:
            object.__setattr__(self, "from_state", LifecycleState.parse(self.from_state).value)
        object.__setattr__(self, "to_state", LifecycleState.parse(self.to_state).value)
        object.__setattr__(self, "reason", _bounded_text(self.reason, 500) or "unspecified")
        object.__setattr__(self, "metadata", _freeze_json(_safe_json(self.metadata)))
        object.__setattr__(self, "previous_hash", _bounded_text(self.previous_hash, 128))
        object.__setattr__(self, "event_hash", _bounded_text(self.event_hash, 128))
        if not self.event_id or not self.lineage_id or not self.instance_id:
            raise LedgerIntegrityError("event identity fields must not be empty")
        if self.generation < 0:
            raise LedgerIntegrityError("event generation must not be negative")
        expected = self.compute_hash()
        if self.event_hash != expected:
            raise LedgerIntegrityError("event hash does not match its contents")

    def payload_for_hash(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "lineage_id": self.lineage_id,
            "generation": self.generation,
            "instance_id": self.instance_id,
            "event_type": self.event_type,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "metadata": _thaw_json(self.metadata),
            "previous_hash": self.previous_hash,
        }

    def compute_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload_for_hash()).encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        lineage_id: str,
        generation: int,
        instance_id: str,
        event_type: str,
        from_state: LifecycleState | str | None,
        to_state: LifecycleState | str,
        reason: str = "unspecified",
        metadata: Mapping[str, Any] | None = None,
        previous_hash: str = LEDGER_GENESIS_HASH,
        timestamp: str | None = None,
        event_id: str | None = None,
    ) -> "LifecycleEvent":
        raw_from = LifecycleState.parse(from_state).value if from_state is not None else None
        raw_to = LifecycleState.parse(to_state).value
        prepared = {
            "sequence": sequence,
            "event_id": event_id or f"life-event-{uuid.uuid4().hex}",
            "timestamp": timestamp or _utc_now(),
            "lineage_id": lineage_id,
            "generation": generation,
            "instance_id": instance_id,
            "event_type": event_type,
            "from_state": raw_from,
            "to_state": raw_to,
            "reason": reason,
            "metadata": _safe_json(metadata or {}),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(_canonical_json(prepared).encode("utf-8")).hexdigest()
        return cls(event_hash=event_hash, **prepared)

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload_for_hash()
        payload["event_hash"] = self.event_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "LifecycleEvent":
        if not isinstance(data, Mapping):
            raise LedgerIntegrityError("ledger event is not an object")
        return cls(
            sequence=data.get("sequence", 0),
            event_id=data.get("event_id", ""),
            timestamp=data.get("timestamp", ""),
            lineage_id=data.get("lineage_id", ""),
            generation=data.get("generation", 0),
            instance_id=data.get("instance_id", ""),
            event_type=data.get("event_type", "lifecycle"),
            from_state=data.get("from_state"),
            to_state=data.get("to_state", ""),
            reason=data.get("reason", "unspecified"),
            metadata=data.get("metadata", {}),
            previous_hash=data.get("previous_hash", LEDGER_GENESIS_HASH),
            event_hash=data.get("event_hash", ""),
        )


# Short name used by the first P1 tests and by embedders.
LedgerEvent = LifecycleEvent


class AppendOnlyLedger:
    """Immutable-view, append-only event sequence."""

    __slots__ = ("_events",)

    def __init__(self, events: Iterable[LifecycleEvent | Mapping[str, Any]] | None = None):
        object.__setattr__(self, "_events", ())
        for raw_event in events or ():
            event = raw_event if isinstance(raw_event, LifecycleEvent) else LifecycleEvent.from_dict(raw_event)
            self._append_existing(event)

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return self._events

    @property
    def head(self) -> LifecycleEvent | None:
        return self._events[-1] if self._events else None

    @property
    def last_sequence(self) -> int:
        return self.head.sequence if self.head else 0

    @property
    def last_hash(self) -> str:
        return self.head.event_hash if self.head else LEDGER_GENESIS_HASH

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)

    def _append_existing(self, event: LifecycleEvent) -> LifecycleEvent:
        expected_sequence = len(self._events) + 1
        expected_previous = self.last_hash
        if event.sequence != expected_sequence:
            raise LedgerIntegrityError(
                f"event sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        if event.previous_hash != expected_previous:
            raise LedgerIntegrityError("event previous hash does not match ledger head")
        if self._events:
            head = self.head
            if (
                event.lineage_id != head.lineage_id
                or event.instance_id != head.instance_id
                or event.generation != head.generation
            ):
                raise LedgerIntegrityError("event identity changed inside one instance ledger")
        object.__setattr__(self, "_events", self._events + (event,))
        return event

    def append(self, **kwargs: Any) -> LifecycleEvent:
        """Create and append one event; no update/delete operation exists."""
        event = LifecycleEvent.create(
            sequence=len(self._events) + 1,
            previous_hash=self.last_hash,
            **kwargs,
        )
        return self._append_existing(event)

    def append_event(self, event: LifecycleEvent | Mapping[str, Any]) -> LifecycleEvent:
        """Append a pre-built event after validating the complete chain."""
        parsed = event if isinstance(event, LifecycleEvent) else LifecycleEvent.from_dict(event)
        return self._append_existing(parsed)

    def verify_chain(self, *, validate_states: bool = True) -> bool:
        probe = AppendOnlyLedger()
        for event in self._events:
            probe._append_existing(event)
        if validate_states and self._events:
            previous = None
            kernel_cls = globals().get("LifeKernel")
            for index, event in enumerate(self._events):
                if index == 0:
                    if event.from_state is not None or event.to_state != LifecycleState.CREATED.value:
                        raise LedgerIntegrityError("ledger genesis must create CREATED")
                else:
                    if event.from_state != previous:
                        raise LedgerIntegrityError("ledger lifecycle state discontinuity")
                    if kernel_cls is not None:
                        allowed = kernel_cls._ALLOWED_TRANSITIONS.get(
                            LifecycleState.parse(previous), frozenset()
                        )
                        if LifecycleState.parse(event.to_state) not in allowed:
                            raise LedgerIntegrityError("ledger contains an illegal lifecycle edge")
                previous = event.to_state
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": LIFE_KERNEL_SCHEMA_VERSION,
            "events": [event.to_dict() for event in self._events],
            "last_sequence": self.last_sequence,
            "last_hash": self.last_hash,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any] | None) -> "AppendOnlyLedger":
        if not isinstance(data, Mapping):
            raise LedgerIntegrityError("ledger snapshot is missing")
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise LedgerIntegrityError("ledger events must be a list")
        ledger = cls(raw_events)
        declared_sequence = data.get("last_sequence")
        if declared_sequence is not None:
            try:
                matches = int(declared_sequence) == ledger.last_sequence
            except (TypeError, ValueError, OverflowError) as exc:
                raise LedgerIntegrityError("ledger last_sequence is invalid") from exc
            if not matches:
                raise LedgerIntegrityError("ledger last_sequence mismatch")
        declared_hash = data.get("last_hash")
        if declared_hash is not None and str(declared_hash) != ledger.last_hash:
            raise LedgerIntegrityError("ledger last_hash mismatch")
        return ledger


class LifeKernel:
    """Constitutional identity + lifecycle state machine."""

    _ALLOWED_TRANSITIONS = MappingProxyType(
        {
            LifecycleState.CREATED: frozenset(
                {LifecycleState.BOOTSTRAPPING, LifecycleState.RETIRED, LifecycleState.DEAD}
            ),
            LifecycleState.BOOTSTRAPPING: frozenset(
                {
                    LifecycleState.ACTIVE,
                    LifecycleState.DEGRADED,
                    LifecycleState.QUARANTINED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.ACTIVE: frozenset(
                {
                    LifecycleState.SLEEPING,
                    LifecycleState.DEGRADED,
                    LifecycleState.QUARANTINED,
                    LifecycleState.SUCCESSION_PENDING,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.SLEEPING: frozenset(
                {
                    LifecycleState.ACTIVE,
                    LifecycleState.DEGRADED,
                    LifecycleState.QUARANTINED,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.DEGRADED: frozenset(
                {
                    LifecycleState.ACTIVE,
                    LifecycleState.SLEEPING,
                    LifecycleState.QUARANTINED,
                    LifecycleState.RECOVERING,
                    LifecycleState.SUCCESSION_PENDING,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.QUARANTINED: frozenset(
                {
                    LifecycleState.RECOVERING,
                    LifecycleState.SUCCESSION_PENDING,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.RECOVERING: frozenset(
                {
                    LifecycleState.ACTIVE,
                    LifecycleState.DEGRADED,
                    LifecycleState.QUARANTINED,
                    LifecycleState.SUCCESSION_PENDING,
                    LifecycleState.RETIRED,
                    LifecycleState.DEAD,
                }
            ),
            LifecycleState.SUCCESSION_PENDING: frozenset(
                {LifecycleState.RETIRED, LifecycleState.DEAD}
            ),
            LifecycleState.RETIRED: frozenset(),
            LifecycleState.DEAD: frozenset(),
        }
    )

    __slots__ = (
        "_identity_core",
        "_identity",
        "_state",
        "_ledger",
        "_event_sink",
        "_lock",
    )

    def __init__(
        self,
        identity_core: IdentityCore | None = None,
        *,
        lineage_id: str | None = None,
        instance_id: str | None = None,
        generation: int = 0,
        parent_instance_id: str | None = None,
        ledger: AppendOnlyLedger | Any | None = None,
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
        lifecycle_state: LifecycleState | str = LifecycleState.CREATED,
    ) -> None:
        durable_adapter = None
        loaded_events: list[LifecycleEvent | Mapping[str, Any]] = []
        # Accept a durable append adapter as a convenience.  The kernel still
        # keeps its own immutable in-memory view; the adapter is only a sink.
        if ledger is not None and not isinstance(ledger, AppendOnlyLedger):
            durable_adapter = ledger
            # ``StateStore`` names the constitutional methods explicitly;
            # keep the generic ``load``/``append`` duck type for the standalone
            # SQLite adapter and small embedders.
            loader = getattr(ledger, "load_life_events", None)
            if not callable(loader):
                loader = getattr(ledger, "load", None)
            if callable(loader):
                try:
                    loaded_events = list(
                        loader(instance_id=instance_id)
                        if instance_id is not None
                        else loader()
                    )
                except TypeError:
                    loaded_events = list(loader())
            else:
                raw_events = getattr(ledger, "events", ())
                loaded_events = list(raw_events() if callable(raw_events) else raw_events or ())
            ledger = AppendOnlyLedger(loaded_events)
            if event_sink is None:
                event_sink = getattr(durable_adapter, "append_life_event", None)
                if not callable(event_sink):
                    event_sink = getattr(durable_adapter, "append", None)

        if identity_core is None:
            inferred_lineage = lineage_id
            inferred_core = None
            if loaded_events:
                inferred_lineage = str(
                    loaded_events[0].get("lineage_id", "")
                    if isinstance(loaded_events[0], Mapping)
                    else getattr(loaded_events[0], "lineage_id", "")
                ) or None
                first_metadata = (
                    loaded_events[0].get("metadata", {})
                    if isinstance(loaded_events[0], Mapping)
                    else getattr(loaded_events[0], "metadata", {})
                )
                if isinstance(first_metadata, Mapping):
                    raw_core = first_metadata.get("identity_core")
                    if isinstance(raw_core, Mapping):
                        inferred_core = IdentityCore.from_dict(raw_core)
            identity_core = inferred_core or IdentityCore.new(lineage_id=inferred_lineage)
            if lineage_id and identity_core.lineage_id != str(lineage_id):
                raise ValueError("lineage_id does not match loaded identity core")
        elif lineage_id and identity_core.lineage_id != str(lineage_id):
            raise ValueError("lineage_id does not match identity core")

        if loaded_events:
            first = loaded_events[0]
            loaded_instance = (
                first.get("instance_id", "")
                if isinstance(first, Mapping)
                else getattr(first, "instance_id", "")
            )
            if loaded_instance and instance_id is None:
                instance_id = str(loaded_instance)
            first_generation = (
                first.get("generation", generation)
                if isinstance(first, Mapping)
                else getattr(first, "generation", generation)
            )
            # A loaded chain is authoritative for its concrete instance.
            generation = int(first_generation)
            last = loaded_events[-1]
            lifecycle_state = (
                last.get("to_state", lifecycle_state)
                if isinstance(last, Mapping)
                else getattr(last, "to_state", lifecycle_state)
            )
        identity = LifeIdentity.new(
            identity_core.lineage_id,
            generation=generation,
            instance_id=instance_id,
            parent_instance_id=parent_instance_id,
        )
        if ledger is None:
            ledger = AppendOnlyLedger()
        if not isinstance(ledger, AppendOnlyLedger):
            raise TypeError("ledger must be AppendOnlyLedger or an append adapter")
        requested_state = LifecycleState.parse(lifecycle_state)
        if not loaded_events and len(ledger) == 0 and requested_state != LifecycleState.CREATED:
            raise LifecycleTransitionError(
                "a fresh life kernel must begin in CREATED"
            )
        object.__setattr__(self, "_identity_core", identity_core)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_state", requested_state)
        object.__setattr__(self, "_ledger", ledger)
        object.__setattr__(self, "_event_sink", None)
        object.__setattr__(self, "_lock", RLock())
        if len(ledger) == 0:
            self._commit_new_event(
                to_state=requested_state,
                from_state=None,
                event_type="created",
                reason="生命个体登记",
                metadata={
                    "genesis": True,
                    "identity_core_fingerprint": identity_core.fingerprint,
                    # Keep a bounded, auditable copy of the constitutional
                    # fields with the first event.  This lets a restart
                    # reconstruct the identity from the durable ledger when
                    # the bounded brain-state snapshot is unavailable.
                    "identity_core": identity_core.to_dict(),
                    "identity": identity.to_dict(),
                },
                sink=None,
            )
        else:
            self._validate_ledger_identity(ledger)
            head_state = LifecycleState.parse(ledger.head.to_state)
            if head_state != requested_state:
                raise LedgerIntegrityError("kernel state does not match ledger head")
        if event_sink is not None:
            try:
                self.attach_event_sink(event_sink)
            except LedgerPersistenceError:
                # Keep the sink attached but leave the kernel in its safe
                # current state.  The next transition will retry and fail
                # closed; callers can explicitly reattach after storage is
                # repaired.  This makes construction side-effect-tolerant
                # without allowing an unpersisted transition.
                object.__setattr__(self, "_event_sink", event_sink)

    @property
    def identity_core(self) -> IdentityCore:
        return self._identity_core

    @property
    def identity_root(self) -> str:
        """Stable string alias for integrations that expose an identity root."""
        return self._identity_core.lineage_id

    @property
    def identity(self) -> LifeIdentity:
        return self._identity

    @property
    def lineage_id(self) -> str:
        return self._identity.lineage_id

    @property
    def generation(self) -> int:
        return self._identity.generation

    @property
    def instance_id(self) -> str:
        return self._identity.instance_id

    @property
    def parent_instance_id(self) -> str | None:
        return self._identity.parent_instance_id

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def current_state(self) -> LifecycleState:
        return self._state

    @property
    def ledger(self) -> AppendOnlyLedger:
        return self._ledger

    @property
    def is_terminal(self) -> bool:
        return self._state in {LifecycleState.RETIRED, LifecycleState.DEAD}

    @property
    def accepts_input(self) -> bool:
        return self._state in {
            LifecycleState.CREATED,
            LifecycleState.ACTIVE,
            LifecycleState.SLEEPING,
            LifecycleState.DEGRADED,
        }

    @property
    def allows_self_modification(self) -> bool:
        return self._state == LifecycleState.ACTIVE

    @classmethod
    def allowed_transitions(cls, state: LifecycleState | str) -> frozenset[LifecycleState]:
        return cls._ALLOWED_TRANSITIONS[LifecycleState.parse(state)]

    def _validate_ledger_identity(self, ledger: AppendOnlyLedger) -> None:
        if not ledger.head:
            return
        for event in ledger.events:
            if (
                event.lineage_id != self.lineage_id
                or event.instance_id != self.instance_id
                or event.generation != self.generation
            ):
                raise LedgerIntegrityError("ledger event identity does not match kernel")
        ledger.verify_chain()
        # The event chain pins the immutable constitutional identity.  Without
        # this check, a caller could edit ``identity_core.purpose`` in a
        # snapshot while leaving the otherwise-valid lifecycle hashes intact.
        genesis_metadata = _thaw_json(ledger.events[0].metadata)
        if not isinstance(genesis_metadata, Mapping) or genesis_metadata.get(
            "identity_core_fingerprint"
        ) != self.identity_core.fingerprint:
            raise LedgerIntegrityError("identity core fingerprint does not match genesis")
        # Pin the complete constitutional identity, not just the lineage and
        # core fingerprint.  In particular, a forged snapshot must not be
        # able to rewrite ``parent_instance_id`` while replaying the same
        # immutable event hashes.
        genesis_core = genesis_metadata.get("identity_core")
        if isinstance(genesis_core, Mapping):
            try:
                if IdentityCore.from_dict(genesis_core).to_dict() != self.identity_core.to_dict():
                    raise LedgerIntegrityError("identity core differs from genesis metadata")
            except LedgerIntegrityError:
                raise
            except Exception as exc:
                raise LedgerIntegrityError("genesis identity core is invalid") from exc
        genesis_identity = genesis_metadata.get("identity")
        if isinstance(genesis_identity, Mapping):
            try:
                if LifeIdentity.from_dict(genesis_identity).to_dict() != self.identity.to_dict():
                    raise LedgerIntegrityError("life identity differs from genesis metadata")
            except LedgerIntegrityError:
                raise
            except Exception as exc:
                raise LedgerIntegrityError("genesis identity is invalid") from exc

    def _commit_new_event(
        self,
        *,
        to_state: LifecycleState,
        from_state: LifecycleState | None,
        event_type: str,
        reason: str,
        metadata: Mapping[str, Any] | None,
        sink: Callable[[dict[str, Any]], Any] | None,
    ) -> LifecycleEvent:
        with self._lock:
            event = LifecycleEvent.create(
                sequence=self._ledger.last_sequence + 1,
                lineage_id=self.lineage_id,
                generation=self.generation,
                instance_id=self.instance_id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                metadata=metadata,
                previous_hash=self._ledger.last_hash,
            )
            # Validate append on a copy before touching the live ledger.  If
            # the sink rejects the event, both state and in-memory history stay
            # put.
            candidate = AppendOnlyLedger(self._ledger.events)
            candidate.append_event(event)
            active_sink = sink if sink is not None else self._event_sink
            if active_sink is not None:
                try:
                    result = active_sink(event.to_dict())
                except Exception as exc:  # pragma: no cover - exact adapter decides
                    raise LedgerPersistenceError("life ledger rejected event") from exc
                if result is not None and not bool(result):
                    raise LedgerPersistenceError("life ledger rejected event")
            object.__setattr__(self, "_ledger", candidate)
            object.__setattr__(self, "_state", to_state)
            return event

    def transition(
        self,
        target: LifecycleState | str,
        reason: str = "unspecified",
        *,
        metadata: Mapping[str, Any] | None = None,
        event_type: str = "lifecycle_transition",
    ) -> LifecycleEvent:
        target_state = LifecycleState.parse(target)
        # Check the edge while holding the same lock used for the commit.  A
        # concurrent caller must observe the newly committed state rather than
        # prepare an event from a stale ``from_state``.
        with self._lock:
            current_state = self._state
            if target_state == current_state:
                raise LifecycleTransitionError(
                    f"duplicate lifecycle state: {current_state.value}"
                )
            allowed = self._ALLOWED_TRANSITIONS[current_state]
            if target_state not in allowed:
                raise LifecycleTransitionError(
                    f"illegal lifecycle transition: {current_state.value} -> {target_state.value}"
                )
            return self._commit_new_event(
                to_state=target_state,
                from_state=current_state,
                event_type=event_type,
                reason=reason,
                metadata=metadata,
                sink=None,
            )

    def try_transition(
        self,
        target: LifecycleState | str,
        reason: str = "unspecified",
        *,
        metadata: Mapping[str, Any] | None = None,
        event_type: str = "lifecycle_transition",
    ) -> bool:
        try:
            self.transition(target, reason, metadata=metadata, event_type=event_type)
            return True
        except LifecycleError:
            return False

    def attach_event_sink(
        self,
        sink: Callable[[dict[str, Any]], Any],
        *,
        replay: bool = True,
    ) -> None:
        """Attach a durable sink and optionally idempotently replay history."""
        if not callable(sink):
            raise TypeError("event sink must be callable")
        if replay:
            for event in self._ledger.events:
                try:
                    result = sink(event.to_dict())
                except Exception as exc:
                    raise LedgerPersistenceError("life ledger history could not be attached") from exc
                if result is not None and not bool(result):
                    raise LedgerPersistenceError("life ledger history could not be attached")
        object.__setattr__(self, "_event_sink", sink)

    def detach_event_sink(self) -> None:
        object.__setattr__(self, "_event_sink", None)

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": LIFE_KERNEL_SCHEMA_VERSION,
            "lineage_id": self.lineage_id,
            "generation": self.generation,
            "instance_id": self.instance_id,
            "parent_instance_id": self.parent_instance_id,
            "lifecycle_state": self.state.value,
            "state": self.state.value,
            "last_sequence": self.ledger.last_sequence,
            "last_hash": self.ledger.last_hash,
            "accepts_input": self.accepts_input,
            "allows_self_modification": self.allows_self_modification,
        }

    def snapshot(self, *, include_events: bool = True) -> dict[str, Any]:
        data = self.public_snapshot()
        data["identity_core"] = self.identity_core.to_dict()
        data["identity"] = self.identity.to_dict()
        if include_events:
            data["ledger"] = self.ledger.snapshot()
        return data

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any] | None,
        *,
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
    ) -> "LifeKernel":
        if not isinstance(data, Mapping):
            raise LedgerIntegrityError("life kernel snapshot is missing")
        core = IdentityCore.from_dict(data.get("identity_core"))
        identity_data = data.get("identity")
        if isinstance(identity_data, Mapping):
            identity = LifeIdentity.from_dict(identity_data)
        else:
            identity = LifeIdentity.from_dict(
                {
                    "lineage_id": data.get("lineage_id", core.lineage_id),
                    "generation": data.get("generation", 0),
                    "instance_id": data.get("instance_id", ""),
                    "parent_instance_id": data.get("parent_instance_id"),
                }
            )
        if identity.lineage_id != core.lineage_id:
            raise LedgerIntegrityError("life identity lineage differs from identity core")
        ledger_data = data.get("ledger")
        if isinstance(ledger_data, Mapping):
            ledger = AppendOnlyLedger.from_snapshot(ledger_data)
        else:
            # A compact projection is not enough to reconstruct history.  Fail
            # closed instead of silently inventing a new identity or history.
            raise LedgerIntegrityError("life kernel snapshot has no ledger history")
        state_value = data.get("lifecycle_state", data.get("state", ""))
        return cls(
            identity_core=core,
            instance_id=identity.instance_id,
            generation=identity.generation,
            parent_instance_id=identity.parent_instance_id,
            ledger=ledger,
            lifecycle_state=state_value,
            event_sink=event_sink,
        )


class SQLiteLedger:
    """Small standalone adapter for callers that do not use ``StateStore``.

    It shares the same table contract as ``StateStore.append_life_event`` and
    intentionally exposes INSERT-only behavior.  Each operation uses a short
    connection under a re-entrant lock, so a caller may safely use the adapter
    from more than one thread without sharing SQLite handles.
    """

    TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS life_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            lineage_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            instance_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            metadata TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            UNIQUE(instance_id, sequence)
        )
    """

    # Keep the standalone adapter subject to the same append-only guarantee
    # as ``StateStore``.  The guard lives in SQLite itself so a caller that
    # obtains a raw connection cannot rewrite constitutional history through
    # UPDATE or DELETE.
    GUARD_SQL = """
        CREATE TRIGGER IF NOT EXISTS life_ledger_no_update
        BEFORE UPDATE ON life_ledger
        BEGIN
            SELECT RAISE(ABORT, 'life_ledger is INSERT-only');
        END;
        CREATE TRIGGER IF NOT EXISTS life_ledger_no_delete
        BEFORE DELETE ON life_ledger
        BEGIN
            SELECT RAISE(ABORT, 'life_ledger is INSERT-only');
        END;
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = RLock()
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_table(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(self.TABLE_SQL)
                conn.executescript(self.GUARD_SQL)
                conn.commit()
            finally:
                conn.close()

    def append(self, event: LifecycleEvent | Mapping[str, Any]) -> bool:
        # Validate the immutable event and its genesis contract before any
        # write.  A correctly-shaped but hash-invalid row must never enter the
        # durable table.
        try:
            parsed = event if isinstance(event, LifecycleEvent) else LifecycleEvent.from_dict(event)
            payload = parsed.to_dict()
            # Only a sequence-1 event can be checked as a standalone ledger;
            # later events are validated against the persisted predecessor
            # below.  Requiring every event to look like a genesis record
            # would incorrectly reject normal transitions during replay.
            if parsed.sequence == 1:
                AppendOnlyLedger([parsed]).verify_chain()
            metadata = _canonical_json(payload.get("metadata", {}))
        except Exception:
            return False
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(self.TABLE_SQL)
                conn.executescript(self.GUARD_SQL)
                # Replays are idempotent only when every immutable field is
                # identical.  Comparing the normalized event projection is
                # stronger than checking event_hash alone.
                existing = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger WHERE event_id = ?",
                    (payload["event_id"],),
                ).fetchone()
                if existing is not None:
                    try:
                        existing_payload = dict(existing)
                        existing_payload["metadata"] = json.loads(existing_payload["metadata"])
                        return LifecycleEvent.from_dict(existing_payload).to_dict() == payload
                    except Exception:
                        return False

                # Build the selected instance chain and append to a probe
                # before touching SQLite.  This rejects sequence gaps,
                # predecessor mismatches, identity changes and illegal state
                # edges at this adapter boundary (not only in LifeKernel).
                try:
                    rows = conn.execute(
                        "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                        "timestamp, event_type, from_state, to_state, reason, metadata, "
                        "previous_hash, event_hash FROM life_ledger "
                        "WHERE instance_id = ? ORDER BY sequence ASC",
                        (payload["instance_id"],),
                    ).fetchall()
                    existing_events: list[LifecycleEvent] = []
                    for row in rows:
                        item = dict(row)
                        item["metadata"] = json.loads(item["metadata"])
                        existing_events.append(LifecycleEvent.from_dict(item))
                    probe = AppendOnlyLedger(existing_events)
                    probe.append_event(parsed)
                    probe.verify_chain()
                except Exception:
                    conn.rollback()
                    return False
                try:
                    conn.execute(
                        """INSERT INTO life_ledger
                        (event_id, lineage_id, generation, instance_id, sequence,
                         timestamp, event_type, from_state, to_state, reason,
                         metadata, previous_hash, event_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                            metadata,
                            payload["previous_hash"],
                            payload["event_hash"],
                        ),
                    )
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                        "timestamp, event_type, from_state, to_state, reason, metadata, "
                        "previous_hash, event_hash FROM life_ledger WHERE event_id = ?",
                        (payload["event_id"],),
                    ).fetchone()
                    conn.rollback()
                    if existing is None:
                        return False
                    try:
                        existing_payload = dict(existing)
                        existing_payload["metadata"] = json.loads(existing_payload["metadata"])
                        return LifecycleEvent.from_dict(existing_payload).to_dict() == payload
                    except Exception:
                        return False
                except Exception:
                    conn.rollback()
                    return False
            finally:
                conn.close()

    def load(
        self,
        *,
        instance_id: str | None = None,
        lineage_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instance_id is not None:
            clauses.append("instance_id = ?")
            params.append(str(instance_id))
        if lineage_id is not None:
            clauses.append("lineage_id = ?")
            params.append(str(lineage_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(self.TABLE_SQL)
                rows = conn.execute(
                    "SELECT event_id, lineage_id, generation, instance_id, sequence, "
                    "timestamp, event_type, from_state, to_state, reason, metadata, "
                    "previous_hash, event_hash FROM life_ledger" + where
                    + " ORDER BY id ASC",
                    params,
                ).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    try:
                        item["metadata"] = json.loads(item["metadata"])
                    except (TypeError, json.JSONDecodeError):
                        item["metadata"] = {}
                    result.append(item)
                return result
            finally:
                conn.close()

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(LifecycleEvent.from_dict(item) for item in self.load())

    def verify_chain(self, *, instance_id: str | None = None) -> bool:
        grouped: dict[str, AppendOnlyLedger] = {}
        for raw in self.load(instance_id=instance_id):
            key = str(raw["instance_id"])
            grouped.setdefault(key, AppendOnlyLedger()).append_event(raw)
        for ledger in grouped.values():
            ledger.verify_chain()
        return True

    def close(self) -> None:
        """Compatibility no-op; connections are deliberately per operation."""
        return None


__all__ = [
    "AppendOnlyLedger",
    "CORE_PURPOSE",
    "IdentityCore",
    "LedgerEvent",
    "LedgerIntegrityError",
    "LedgerPersistenceError",
    "LifeIdentity",
    "LifeKernel",
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleState",
    "LifecycleTransitionError",
    "SQLiteLedger",
]
