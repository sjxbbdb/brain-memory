"""有界资源稳态与受控环境适配器。

The homeostasis layer is deliberately kept outside :mod:`brain.life_kernel`.
The kernel decides whether an organism is allowed to live; this module
measures pressure on that organism and turns it into a conservative, auditable
decision.  It also provides the only small environment seam used by candidate
code.  No method in this module opens a network connection, executes a
process, or writes a file on behalf of its caller.

Two rules are important here:

* a resource observation is evidence, not an instruction.  Values are bounded,
  validated, and recorded in an append-only hash chain;
* an environment approval is scoped to one immutable request.  Approval
  tokens are accepted by a validator but are never persisted in snapshots or
  audit records.

The implementation uses only the Python standard library and is safe to use
in a long-running process.  It is an accounting and policy boundary, not a
replacement for an operating-system sandbox.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping
import uuid


HOMEOSTASIS_SCHEMA_VERSION = 1
RESOURCE_LEDGER_SCHEMA_VERSION = 1
ENVIRONMENT_SCHEMA_VERSION = 1

# Additive values are accumulated during a bounded tick window.  Gauge values
# describe the latest observed level and therefore must not be accumulated.
ADDITIVE_RESOURCES = frozenset({
    "compute_ms",
    "network_calls",
    "model_tokens",
    "action_risk",
})
GAUGE_RESOURCES = frozenset({
    "memory_mb",
    "storage_mb",
    "attention_load",
})
KNOWN_RESOURCES = ADDITIVE_RESOURCES | GAUGE_RESOURCES


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 500, default: str = "") -> str:
    try:
        text = default if value is None else str(value)
    except Exception:
        text = default
    # NUL/control characters make line-oriented audit consumers ambiguous.
    return "".join(ch if ord(ch) >= 32 or ch in "\r\n\t" else " " for ch in text)[:limit]


def _finite_number(value: Any, *, name: str, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    # Avoid surprising -0.0 and excessive precision in hashes.
    return 0.0 if number == 0 else number


def _bounded_int(value: Any, *, name: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{name} outside allowed range")
    return number


def _bool_flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any, depth: int = 0) -> Any:
    """Bound and recursively freeze JSON-like values."""
    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, str)):
        return _text(value, 500) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        return MappingProxyType({
            _text(key, 80): _freeze(item, depth + 1)
            for key, item in list(value.items())[:64]
        })
    if isinstance(value, (tuple, list, set)):
        return tuple(_freeze(item, depth + 1) for item in list(value)[:64])
    return _text(value, 500)


def _canonical(value: Any) -> str:
    return json.dumps(_thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class HomeostasisAction(str, Enum):
    """Conservative response to observed resource pressure."""

    CONTINUE = "CONTINUE"
    THROTTLE = "THROTTLE"
    SLEEP = "SLEEP"
    DEGRADE = "DEGRADE"
    QUARANTINE = "QUARANTINE"

    @classmethod
    def parse(cls, value: "HomeostasisAction | str") -> "HomeostasisAction":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"unknown homeostasis action: {value!r}") from exc


class ResourceLedgerCapacityError(RuntimeError):
    """The bounded evidence ledger cannot accept another event."""


class EnvironmentKind(str, Enum):
    """Whether an adapter is being used for a disposable evaluation world."""

    EVALUATION = "evaluation"
    LIVING = "living"

    @classmethod
    def parse(cls, value: "EnvironmentKind | str") -> "EnvironmentKind":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown environment kind: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Immutable per-window limits for the organism.

    ``limits`` is intentionally a mapping rather than a fixed set so a host
    can add a measured dimension without changing the policy object.  Unknown
    dimensions are accepted when they have a finite positive limit; the
    controller will account for them as additive values.
    """

    limits: Mapping[str, float] = field(default_factory=lambda: {
        # Five minutes of measured heartbeat compute per 100-tick window is a
        # conservative default for the two-second loop; embedders should set
        # a tighter budget from their measured deployment envelope.
        "compute_ms": 300_000.0,
        "memory_mb": 512.0,
        "storage_mb": 4_096.0,
        "network_calls": 100.0,
        "model_tokens": 50_000.0,
        "action_risk": 10.0,
        "attention_load": 1.0,
    })
    warning_ratio: float = 0.70
    critical_ratio: float = 0.90
    window_ticks: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Mapping) or not self.limits:
            raise ValueError("limits must be a non-empty mapping")
        clean: dict[str, float] = {}
        for raw_name, raw_limit in list(self.limits.items())[:64]:
            name = _text(raw_name, 80).strip()
            if not name or "\x00" in name:
                raise ValueError("resource names must be non-empty")
            if name in clean:
                raise ValueError(f"duplicate resource: {name}")
            clean[name] = _finite_number(raw_limit, name=f"limit[{name}]")
            if clean[name] <= 0:
                raise ValueError(f"limit[{name}] must be > 0")
        warning = _finite_number(self.warning_ratio, name="warning_ratio")
        critical = _finite_number(self.critical_ratio, name="critical_ratio")
        if not 0.0 < warning < critical <= 1.0:
            raise ValueError("warning_ratio and critical_ratio must satisfy 0 < warning < critical <= 1")
        ticks = _bounded_int(self.window_ticks, name="window_ticks", minimum=1, maximum=10**7)
        object.__setattr__(self, "limits", MappingProxyType(clean))
        object.__setattr__(self, "warning_ratio", warning)
        object.__setattr__(self, "critical_ratio", critical)
        object.__setattr__(self, "window_ticks", ticks)

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict())

    def limit_for(self, name: str) -> float | None:
        return self.limits.get(str(name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": {str(k): float(v) for k, v in self.limits.items()},
            "warning_ratio": self.warning_ratio,
            "critical_ratio": self.critical_ratio,
            "window_ticks": self.window_ticks,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ResourceBudget":
        if not isinstance(data, Mapping):
            raise ValueError("budget snapshot must be a mapping")
        return cls(
            limits=data.get("limits", {}),
            warning_ratio=data.get("warning_ratio", 0.70),
            critical_ratio=data.get("critical_ratio", 0.90),
            window_ticks=data.get("window_ticks", 100),
        )


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    """One bounded measurement supplied by a trusted runtime adapter."""

    tick: int
    usage: Mapping[str, float]
    source: str = "runtime"
    context: str = ""
    timestamp: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        tick = _bounded_int(self.tick, name="tick")
        if not isinstance(self.usage, Mapping):
            raise ValueError("usage must be a mapping")
        clean: dict[str, float] = {}
        for raw_name, raw_value in list(self.usage.items())[:64]:
            name = _text(raw_name, 80).strip()
            if not name:
                continue
            clean[name] = _finite_number(raw_value, name=f"usage[{name}]")
        object.__setattr__(self, "tick", tick)
        object.__setattr__(self, "usage", MappingProxyType(clean))
        object.__setattr__(self, "source", _text(self.source, 120, "runtime"))
        object.__setattr__(self, "context", _text(self.context, 500))
        object.__setattr__(self, "timestamp", _text(self.timestamp, 80, _utc_now()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "usage": dict(self.usage),
            "source": self.source,
            "context": self.context,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceObservation":
        if not isinstance(data, Mapping):
            raise ValueError("observation must be a mapping")
        return cls(
            tick=data.get("tick", 0),
            usage=data.get("usage", {}),
            source=data.get("source", "runtime"),
            context=data.get("context", ""),
            timestamp=data.get("timestamp", _utc_now()),
        )


@dataclass(frozen=True, slots=True)
class HomeostasisDecision:
    """Policy result for one observation; it grants no external permission."""

    action: HomeostasisAction
    reason: str
    tick: int
    ratios: Mapping[str, float] = field(default_factory=dict)
    exceeded: tuple[str, ...] = field(default_factory=tuple)
    timestamp: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        action = HomeostasisAction.parse(self.action)
        tick = _bounded_int(self.tick, name="decision.tick")
        ratios: dict[str, float] = {}
        if isinstance(self.ratios, Mapping):
            for key, value in list(self.ratios.items())[:64]:
                ratios[_text(key, 80)] = _finite_number(value, name=f"ratio[{key}]")
        exceeded = tuple(_text(item, 80) for item in list(self.exceeded)[:64] if _text(item, 80))
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "reason", _text(self.reason, 500))
        object.__setattr__(self, "tick", tick)
        object.__setattr__(self, "ratios", MappingProxyType(ratios))
        object.__setattr__(self, "exceeded", exceeded)
        object.__setattr__(self, "timestamp", _text(self.timestamp, 80, _utc_now()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "tick": self.tick,
            "ratios": dict(self.ratios),
            "exceeded": list(self.exceeded),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HomeostasisDecision":
        if not isinstance(data, Mapping):
            raise ValueError("decision must be a mapping")
        return cls(
            action=data.get("action", HomeostasisAction.CONTINUE.value),
            reason=data.get("reason", ""),
            tick=data.get("tick", 0),
            ratios=data.get("ratios", {}),
            exceeded=data.get("exceeded", []),
            timestamp=data.get("timestamp", _utc_now()),
        )


@dataclass(frozen=True, slots=True)
class ResourceEvent:
    """Hash-chained observation and decision pair."""

    sequence: int
    previous_hash: str
    observation: ResourceObservation
    decision: HomeostasisDecision
    event_hash: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def create(
        cls,
        sequence: int,
        previous_hash: str,
        observation: ResourceObservation,
        decision: HomeostasisDecision,
        event_id: str | None = None,
    ) -> "ResourceEvent":
        seq = _bounded_int(sequence, name="sequence", minimum=0)
        prev = _text(previous_hash, 64)
        eid = _text(event_id, 80) if event_id else uuid.uuid4().hex
        payload = {
            "sequence": seq,
            "previous_hash": prev,
            "observation": observation.to_dict(),
            "decision": decision.to_dict(),
            "event_id": eid,
        }
        return cls(seq, prev, observation, decision, _sha256(payload), eid)

    def payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "observation": self.observation.to_dict(),
            "decision": self.decision.to_dict(),
            "event_id": self.event_id,
        }

    def verify(self, expected_sequence: int, expected_previous_hash: str) -> bool:
        if self.sequence != expected_sequence or self.previous_hash != expected_previous_hash:
            return False
        return self.event_hash == _sha256(self.payload())

    def to_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["event_hash"] = self.event_hash
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceEvent":
        if not isinstance(data, Mapping):
            raise ValueError("resource event must be a mapping")
        observation = ResourceObservation.from_dict(data.get("observation", {}))
        decision = HomeostasisDecision.from_dict(data.get("decision", {}))
        event = cls.create(
            sequence=data.get("sequence", 0),
            previous_hash=data.get("previous_hash", ""),
            observation=observation,
            decision=decision,
            event_id=data.get("event_id"),
        )
        supplied = _text(data.get("event_hash"), 64)
        if not supplied or supplied != event.event_hash:
            raise ValueError("resource event hash mismatch")
        return event


class ResourceLedger:
    """Thread-safe append-only resource evidence ledger."""

    def __init__(
        self,
        events: Iterable[ResourceEvent] | None = None,
        *,
        max_events: int = 4_096,
    ) -> None:
        self.max_events = _bounded_int(max_events, name="max_events", minimum=1, maximum=100_000)
        self._lock = RLock()
        self._events: list[ResourceEvent] = []
        for event in events or ():
            if not isinstance(event, ResourceEvent):
                raise TypeError("events must contain ResourceEvent values")
            if len(self._events) >= self.max_events:
                raise ResourceLedgerCapacityError("resource ledger capacity exceeded")
            self._events.append(event)
        if not self.verify_chain():
            raise ValueError("invalid resource ledger chain")

    @property
    def events(self) -> tuple[ResourceEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def head_hash(self) -> str:
        with self._lock:
            return self._events[-1].event_hash if self._events else ""

    def append(self, observation: ResourceObservation, decision: HomeostasisDecision) -> ResourceEvent:
        if not isinstance(observation, ResourceObservation) or not isinstance(decision, HomeostasisDecision):
            raise TypeError("observation and decision types are required")
        with self._lock:
            if len(self._events) >= self.max_events:
                raise ResourceLedgerCapacityError("resource ledger capacity exceeded")
            event = ResourceEvent.create(
                len(self._events), self.head_hash, observation, decision
            )
            self._events.append(event)
            return event

    def verify_chain(self) -> bool:
        with self._lock:
            previous = ""
            for sequence, event in enumerate(self._events):
                if not event.verify(sequence, previous):
                    return False
                previous = event.event_hash
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self.verify_chain():
                raise ValueError("cannot snapshot an invalid resource ledger")
            return {
                "schema_version": RESOURCE_LEDGER_SCHEMA_VERSION,
                "max_events": self.max_events,
                "events": [event.to_dict() for event in self._events],
                "head_hash": self.head_hash,
            }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any] | None) -> "ResourceLedger":
        if not isinstance(data, Mapping):
            raise ValueError("resource ledger snapshot must be a mapping")
        schema = data.get("schema_version", RESOURCE_LEDGER_SCHEMA_VERSION)
        if schema != RESOURCE_LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported resource ledger schema")
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("resource ledger events must be a bounded list")
        max_events = _bounded_int(
            data.get("max_events", max(4_096, len(raw_events) or 1)),
            name="max_events",
            minimum=1,
            maximum=100_000,
        )
        if len(raw_events) > max_events:
            raise ValueError("resource ledger events must be a bounded list")
        events = [ResourceEvent.from_dict(item) for item in raw_events]
        ledger = cls(events, max_events=max_events)
        supplied_head = _text(data.get("head_hash"), 64)
        if supplied_head != ledger.head_hash:
            raise ValueError("resource ledger head hash mismatch")
        return ledger


class HomeostasisController:
    """Account for observations and choose a latched safe response."""

    def __init__(
        self,
        budget: ResourceBudget | None = None,
        ledger: ResourceLedger | None = None,
        *,
        max_events: int = 4_096,
    ) -> None:
        # An intentionally supplied empty ledger is still the caller's
        # continuity object; truthiness must not silently replace it.
        self.budget = budget if budget is not None else ResourceBudget()
        self.ledger = ledger if ledger is not None else ResourceLedger(max_events=max_events)
        if not self.ledger.verify_chain():
            raise ValueError("invalid resource ledger")
        self._usage: dict[str, float] = {}
        self._window_start_tick: int | None = None
        self._last_tick: int | None = None
        self._quarantine_latched = False
        self._last_decision: HomeostasisDecision | None = None
        self._lock = RLock()
        # Replaying the ledger is deterministic and prevents a snapshot from
        # silently claiming a different accounting state.
        for event in self.ledger.events:
            self._replay_event(event)
        # A legacy snapshot may already have filled the bounded ledger without
        # recording an explicit capacity event.  Treat that as a conservative
        # quarantine on restore; never keep accepting unjournaled observations.
        if len(self.ledger.events) >= self.ledger.max_events and not self._quarantine_latched:
            self._quarantine_latched = True
            self._last_decision = self._capacity_decision(
                tick=self._last_tick if self._last_tick is not None else 0
            )

    @property
    def quarantine_latched(self) -> bool:
        with self._lock:
            return self._quarantine_latched

    @property
    def usage(self) -> Mapping[str, float]:
        with self._lock:
            return MappingProxyType(dict(self._usage))

    @property
    def window_start_tick(self) -> int | None:
        return self._window_start_tick

    @property
    def last_decision(self) -> HomeostasisDecision | None:
        return self._last_decision

    def _replay_event(self, event: ResourceEvent) -> None:
        observation = event.observation
        if self._last_tick is not None and observation.tick < self._last_tick:
            raise ValueError("resource observations must be monotonic")
        if self._window_start_tick is None or observation.tick - self._window_start_tick >= self.budget.window_ticks:
            self._usage = {}
            self._window_start_tick = observation.tick
        self._apply_usage(observation.usage)
        self._last_tick = observation.tick
        self._last_decision = event.decision
        if event.decision.action is HomeostasisAction.QUARANTINE:
            self._quarantine_latched = True

    def _apply_usage(self, observed: Mapping[str, float]) -> None:
        names = set(self.budget.limits) | set(observed)
        for name in names:
            if name not in observed:
                continue
            value = float(observed[name])
            if not math.isfinite(value) or value < 0.0:
                # ResourceObservation normally enforces this at construction,
                # but keep the accounting boundary defensive for adapters that
                # provide a mutable Mapping implementation.
                raise ValueError(f"usage[{name}] must be finite and >= 0")
            if name in GAUGE_RESOURCES:
                candidate = value
            else:
                candidate = self._usage.get(name, 0.0) + value
            # A finite observation can still overflow when additive values
            # are accumulated.  Never retain inf/NaN: the caller's observe()
            # transaction will restore the prior accounting state and reject
            # this observation.
            if not math.isfinite(candidate):
                raise ValueError(f"usage[{name}] overflowed finite range")
            self._usage[name] = candidate

    def _decide(self, observation: ResourceObservation) -> HomeostasisDecision:
        ratios = {
            name: (self._usage.get(name, 0.0) / limit)
            for name, limit in self.budget.limits.items()
        }
        # Unknown observed resources are not ignored: without a declared
        # budget they cannot be safely acted upon, so quarantine is required.
        unknown = sorted(name for name in self._usage if name not in self.budget.limits)
        exceeded = sorted(name for name, ratio in ratios.items() if ratio >= 1.0)
        critical = sorted(name for name, ratio in ratios.items() if ratio >= self.budget.critical_ratio)
        warning = sorted(name for name, ratio in ratios.items() if ratio >= self.budget.warning_ratio)
        if (
            self._quarantine_latched
            or unknown
            or "action_risk" in exceeded
            or "action_risk" in critical
        ):
            action = HomeostasisAction.QUARANTINE
            reason = "resource quarantine latched" if self._quarantine_latched else (
                "undeclared resource observed" if unknown else "action risk budget exceeded"
            )
        elif exceeded:
            # A hard capacity breach is a bounded failure.  Sleep for purely
            # compute/network pressure, degrade for persistent state pressure.
            sleep_only = set(exceeded).issubset({"compute_ms", "network_calls", "model_tokens"})
            action = HomeostasisAction.SLEEP if sleep_only else HomeostasisAction.DEGRADE
            reason = "resource budget exceeded: " + ", ".join(exceeded)
        elif critical:
            sleep_only = set(critical).issubset(
                {"compute_ms", "network_calls", "model_tokens"}
            )
            action = HomeostasisAction.SLEEP if sleep_only else HomeostasisAction.DEGRADE
            reason = "resource pressure critical: " + ", ".join(critical)
        elif warning:
            action = HomeostasisAction.THROTTLE
            reason = "resource pressure warning: " + ", ".join(warning)
        else:
            action = HomeostasisAction.CONTINUE
            reason = "resource usage within budget"
        if action is HomeostasisAction.QUARANTINE:
            self._quarantine_latched = True
        return HomeostasisDecision(
            action=action,
            reason=reason,
            tick=observation.tick,
            ratios=ratios,
            exceeded=tuple(exceeded),
        )

    def _capacity_decision(self, *, tick: int) -> HomeostasisDecision:
        """Return the stable fail-closed decision used after ledger saturation."""

        ratios = {
            name: (self._usage.get(name, 0.0) / limit)
            for name, limit in self.budget.limits.items()
        }
        return HomeostasisDecision(
            action=HomeostasisAction.QUARANTINE,
            reason="resource evidence ledger capacity exhausted; quarantine required",
            tick=_bounded_int(tick, name="tick", minimum=0),
            ratios=ratios,
            exceeded=("ledger_events",),
        )

    def observe(self, observation: ResourceObservation) -> HomeostasisDecision:
        if not isinstance(observation, ResourceObservation):
            raise TypeError("observe expects ResourceObservation")
        with self._lock:
            if self._last_tick is not None and observation.tick < self._last_tick:
                raise ValueError("resource observations must be monotonic")
            # Reserve the final slot for a terminal capacity observation.  A
            # bounded ledger must fail closed exactly once, not raise on every
            # subsequent heartbeat and flood logs while leaving the lifecycle
            # unaware of the safety condition.
            if len(self.ledger.events) >= self.ledger.max_events:
                if not self._quarantine_latched:
                    self._quarantine_latched = True
                    self._last_decision = self._capacity_decision(
                        tick=observation.tick
                    )
                return self._last_decision or self._capacity_decision(
                    tick=observation.tick
                )
            previous_usage = dict(self._usage)
            previous_window = self._window_start_tick
            previous_last_tick = self._last_tick
            previous_latched = self._quarantine_latched
            previous_decision = self._last_decision
            try:
                if self._window_start_tick is None or observation.tick - self._window_start_tick >= self.budget.window_ticks:
                    self._usage = {}
                    self._window_start_tick = observation.tick
                self._apply_usage(observation.usage)
                capacity_terminal = len(self.ledger.events) >= self.ledger.max_events - 1
                if capacity_terminal:
                    self._quarantine_latched = True
                    decision = self._capacity_decision(tick=observation.tick)
                else:
                    decision = self._decide(observation)
                self.ledger.append(observation, decision)
            except Exception:
                # Ledger persistence is part of the safety decision.  Do not
                # leave an unjournaled usage mutation behind when accounting,
                # decision construction, or ledger persistence fails.  In
                # particular, an additive float overflow must not poison the
                # controller with an unjournaled inf/NaN value.
                self._usage = previous_usage
                self._window_start_tick = previous_window
                self._last_tick = previous_last_tick
                self._quarantine_latched = previous_latched
                self._last_decision = previous_decision
                raise
            self._last_tick = observation.tick
            self._last_decision = decision
            return decision

    def clear_quarantine(self, *, verified: bool = False) -> None:
        """Clear a latch only after an independent verifier has attested safety."""
        if not verified:
            raise PermissionError("independent verification is required to clear quarantine")
        with self._lock:
            self._quarantine_latched = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                for value in self._usage.values()
            ):
                raise ValueError("cannot snapshot non-finite resource usage")
            return {
                "schema_version": HOMEOSTASIS_SCHEMA_VERSION,
                "budget": self.budget.to_dict(),
                "budget_fingerprint": self.budget.fingerprint,
                "usage": dict(self._usage),
                "window_start_tick": self._window_start_tick,
                "last_tick": self._last_tick,
                "quarantine_latched": self._quarantine_latched,
                "last_decision": self._last_decision.to_dict() if self._last_decision else None,
                "ledger": self.ledger.snapshot(),
            }

    def public_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe inspection projection (no approval material)."""
        with self._lock:
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                for value in self._usage.values()
            ):
                raise ValueError("cannot project non-finite resource usage")
            return {
                "schema_version": HOMEOSTASIS_SCHEMA_VERSION,
                "budget_fingerprint": self.budget.fingerprint,
                "usage": dict(self._usage),
                "window_start_tick": self._window_start_tick,
                "last_tick": self._last_tick,
                "quarantine_latched": self._quarantine_latched,
                "last_action": self._last_decision.action.value if self._last_decision else None,
                "ledger_head": self.ledger.head_hash,
                "ledger_length": len(self.ledger.events),
            }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "HomeostasisController":
        if not isinstance(data, Mapping):
            raise ValueError("homeostasis snapshot must be a mapping")
        if data.get("schema_version", HOMEOSTASIS_SCHEMA_VERSION) != HOMEOSTASIS_SCHEMA_VERSION:
            raise ValueError("unsupported homeostasis schema")
        budget = ResourceBudget.from_dict(data.get("budget", {}))
        supplied_fp = _text(data.get("budget_fingerprint"), 64)
        if supplied_fp and supplied_fp != budget.fingerprint:
            raise ValueError("resource budget fingerprint mismatch")
        ledger = ResourceLedger.from_snapshot(data.get("ledger", {}))
        controller = cls(budget=budget, ledger=ledger)
        # The ledger is authoritative.  Snapshot projections must match it;
        # accepting a divergent projection would make recovery non-auditable.
        supplied_usage = data.get("usage", {})
        if isinstance(supplied_usage, Mapping):
            clean = {
                str(k): _finite_number(v, name=f"usage[{k}]")
                for k, v in supplied_usage.items()
            }
            if clean != dict(controller._usage):
                raise ValueError("resource usage projection mismatch")
        if data.get("quarantine_latched", controller._quarantine_latched) and not controller._quarantine_latched:
            # A manually latched quarantine is safe to preserve.
            controller._quarantine_latched = True
        elif controller._quarantine_latched and data.get("quarantine_latched") is False:
            raise ValueError("snapshot cannot clear a quarantined controller")
        return controller


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Immutable request presented to a controlled environment."""

    action: str
    target: str
    read_only: bool = True
    uses_network: bool = False
    approval_token: str | None = None

    def __post_init__(self) -> None:
        action = _text(self.action, 120).strip()
        target = _text(self.target, 2_000).strip()
        if not action or not target:
            raise ValueError("action and target are required")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "read_only", _bool_flag(self.read_only, True))
        object.__setattr__(self, "uses_network", _bool_flag(self.uses_network, False))
        if self.approval_token is not None:
            object.__setattr__(self, "approval_token", _text(self.approval_token, 512))

    @property
    def request_hash(self) -> str:
        # Deliberately exclude the token: the hash can safely be persisted and
        # independently bound to the exact action/target/flags.
        return _sha256({
            "action": self.action,
            "target": self.target,
            "read_only": self.read_only,
            "uses_network": self.uses_network,
        })

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = {
            "action": self.action,
            "target": self.target,
            "read_only": self.read_only,
            "uses_network": self.uses_network,
        }
        if not redact and self.approval_token is not None:
            data["approval_token"] = self.approval_token
        return data


@dataclass(frozen=True, slots=True)
class ActionDecision:
    allowed: bool
    reason: str
    request_hash: str
    environment: EnvironmentKind
    timestamp: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", _text(self.reason, 500))
        object.__setattr__(self, "request_hash", _text(self.request_hash, 64))
        object.__setattr__(self, "environment", EnvironmentKind.parse(self.environment))
        object.__setattr__(self, "timestamp", _text(self.timestamp, 80, _utc_now()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "request_hash": self.request_hash,
            "environment": self.environment.value,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentAudit:
    request_hash: str
    allowed: bool
    reason: str
    environment: EnvironmentKind
    action: str
    target_scope: str
    timestamp: str = field(default_factory=_utc_now)
    # Redacted request material retained solely so request_hash can be
    # independently recomputed after a restart.  Approval tokens are never
    # included here.
    request_target: str = ""
    request_read_only: bool = True
    request_uses_network: bool = False
    previous_hash: str = ""
    audit_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_hash", _text(self.request_hash, 64))
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", _text(self.reason, 500))
        object.__setattr__(self, "environment", EnvironmentKind.parse(self.environment))
        object.__setattr__(self, "action", _text(self.action, 120))
        object.__setattr__(self, "target_scope", _text(self.target_scope, 2_000))
        object.__setattr__(self, "timestamp", _text(self.timestamp, 80, _utc_now()))
        object.__setattr__(self, "request_target", _text(self.request_target, 2_000).strip())
        object.__setattr__(self, "request_read_only", _bool_flag(self.request_read_only, True))
        object.__setattr__(self, "request_uses_network", _bool_flag(self.request_uses_network, False))
        previous = _text(self.previous_hash, 64).strip()
        if previous and (len(previous) != 64 or any(c not in "0123456789abcdef" for c in previous.lower())):
            raise ValueError("environment audit previous_hash is malformed")
        object.__setattr__(self, "previous_hash", previous)
        if not self.request_target:
            # Preserve construction compatibility for embedders that created
            # the pre-integrity audit shape directly.  Such an entry is
            # intentionally untrusted (verify() will fail) and cannot cross
            # the strict from_snapshot boundary.
            object.__setattr__(self, "audit_hash", "")
            return
        request_hash = self.request_hash.lower()
        if len(request_hash) != 64 or any(c not in "0123456789abcdef" for c in request_hash):
            raise ValueError("environment audit request_hash is malformed")
        object.__setattr__(self, "request_hash", request_hash)
        expected_request = _sha256({
            "action": self.action,
            "target": self.request_target,
            "read_only": self.request_read_only,
            "uses_network": self.request_uses_network,
        })
        if expected_request != self.request_hash:
            raise ValueError("environment audit request hash mismatch")
        supplied_audit = _text(self.audit_hash, 64).strip().lower()
        expected_audit = self.compute_hash()
        if supplied_audit and supplied_audit != expected_audit:
            raise ValueError("environment audit hash mismatch")
        object.__setattr__(self, "audit_hash", expected_audit)

    def payload_for_hash(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "allowed": self.allowed,
            "reason": self.reason,
            "environment": self.environment.value,
            "action": self.action,
            "target_scope": self.target_scope,
            "timestamp": self.timestamp,
            "request_target": self.request_target,
            "request_read_only": self.request_read_only,
            "request_uses_network": self.request_uses_network,
            "previous_hash": self.previous_hash,
        }

    def compute_hash(self) -> str:
        return _sha256(self.payload_for_hash())

    def verify(self, previous_hash: str = "") -> bool:
        return (
            self.previous_hash == previous_hash
            and self.compute_hash() == self.audit_hash
            and self.request_hash == _sha256({
                "action": self.action,
                "target": self.request_target,
                "read_only": self.request_read_only,
                "uses_network": self.request_uses_network,
            })
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_hash": self.request_hash,
            "allowed": self.allowed,
            "reason": self.reason,
            "environment": self.environment.value,
            "action": self.action,
            "target_scope": self.target_scope,
            "timestamp": self.timestamp,
            "request_target": self.request_target,
            "request_read_only": self.request_read_only,
            "request_uses_network": self.request_uses_network,
            "previous_hash": self.previous_hash,
            "audit_hash": self.audit_hash,
        }


ApprovalValidator = Callable[[ActionRequest, str], bool]


class ControlledEnvironment:
    """Small, deny-by-default policy adapter for evaluation and living worlds."""

    def __init__(
        self,
        *,
        kind: EnvironmentKind | str,
        root: str | os.PathLike[str],
        network_enabled: bool = False,
        approval_validator: ApprovalValidator | None = None,
        audit_limit: int = 1_024,
        expected_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.kind = EnvironmentKind.parse(kind)
        root_path = Path(root).expanduser().resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir():
            raise ValueError("environment root must be an existing directory")
        if expected_root is not None:
            expected_path = Path(expected_root).expanduser().resolve(strict=False)
            if expected_path != root_path:
                raise ValueError("environment root does not match expected_root")
        self.root = root_path
        self.network_enabled = bool(network_enabled)
        self.approval_validator = approval_validator
        self.audit_limit = _bounded_int(audit_limit, name="audit_limit", minimum=1, maximum=100_000)
        self._audit: deque[EnvironmentAudit] = deque(maxlen=self.audit_limit)
        self._lock = RLock()

    @property
    def audit(self) -> tuple[EnvironmentAudit, ...]:
        with self._lock:
            return tuple(self._audit)

    def _inside_root(self, target: str) -> bool:
        try:
            candidate = self._resolve_candidate(target)
            return os.path.commonpath((str(self.root), str(candidate))) == str(self.root)
        except (OSError, RuntimeError, ValueError):
            return False

    def resolve_path(self, target: str) -> Path:
        """Resolve a path only when it remains beneath the environment root."""
        candidate = self._resolve_candidate(target)
        if os.path.commonpath((str(self.root), str(candidate))) != str(self.root):
            raise PermissionError("target is outside the controlled environment")
        return candidate

    def _resolve_candidate(self, target: str) -> Path:
        """Resolve relative paths against the controlled root, never CWD."""
        raw = Path(target).expanduser()
        if not raw.is_absolute():
            raw = self.root / raw
        return raw.resolve(strict=False)

    @staticmethod
    def _is_write(request: ActionRequest) -> bool:
        if not request.read_only:
            return True
        action = request.action.lower()
        if any(word in action for word in (
            "write", "delete", "remove", "rename", "move", "mkdir", "patch",
            "execute", "exec", "spawn", "run", "shell", "publish", "send",
        )):
            return True
        # In the living world, an unknown non-read operation is treated as a
        # write until an operator explicitly approves it.  Evaluation worlds
        # remain useful for candidate read-only probes.
        return False

    def _scope(self, request: ActionRequest) -> str:
        if request.uses_network:
            # Keep URLs visible for audit diagnosis, but never include a token.
            return _text(request.target, 2_000)
        try:
            return str(self.resolve_path(request.target))
        except (PermissionError, OSError, RuntimeError, ValueError):
            return "<outside-root>"

    def _decision(self, request: ActionRequest, allowed: bool, reason: str) -> ActionDecision:
        return ActionDecision(
            allowed=allowed,
            reason=reason,
            request_hash=request.request_hash,
            environment=self.kind,
        )

    def authorize(self, request: ActionRequest) -> ActionDecision:
        """Evaluate one request and append a token-free audit record."""
        if not isinstance(request, ActionRequest):
            raise TypeError("authorize expects ActionRequest")
        reason = "allowed"
        allowed = True
        action_lower = request.action.lower()
        declared_read_operation = any(
            marker in action_lower
            for marker in ("read", "get", "list", "stat", "inspect", "query", "observe")
        )
        write_request = self._is_write(request)
        if self.kind is EnvironmentKind.LIVING and request.read_only and not declared_read_operation:
            # ``read_only=True`` is not enough to bless an unfamiliar verb in
            # a living world; require an explicit approval path instead.
            write_request = True
        if request.uses_network:
            if not self.network_enabled:
                allowed, reason = False, "network disabled in controlled environment"
            elif write_request and self.kind is EnvironmentKind.LIVING:
                allowed = False
                reason = "living-world external writes require approval"
        else:
            # All filesystem-like requests, including reads, stay inside root.
            if not self._inside_root(request.target):
                allowed, reason = False, "target outside controlled root"
            elif write_request and self.kind is EnvironmentKind.LIVING:
                token = request.approval_token
                if not token or self.approval_validator is None:
                    allowed, reason = False, "living-world writes require bound approval"
                else:
                    try:
                        approved = bool(self.approval_validator(request, token))
                    except Exception:
                        approved = False
                    if not approved:
                        allowed, reason = False, "approval validator rejected request"
        decision = self._decision(request, allowed, reason)
        with self._lock:
            record = EnvironmentAudit(
                request_hash=decision.request_hash,
                allowed=decision.allowed,
                reason=decision.reason,
                environment=self.kind,
                action=request.action,
                target_scope=self._scope(request),
                request_target=request.target,
                request_read_only=request.read_only,
                request_uses_network=request.uses_network,
                previous_hash=self._audit[-1].audit_hash if self._audit else "",
            )
            self._audit.append(record)
        return decision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": ENVIRONMENT_SCHEMA_VERSION,
                "kind": self.kind.value,
                "root": str(self.root),
                "root_fingerprint": _sha256({"root": str(self.root)}),
                "network_enabled": self.network_enabled,
                "audit_limit": self.audit_limit,
                "audit_limit_fingerprint": _sha256({"audit_limit": self.audit_limit}),
                "audit": [entry.to_dict() for entry in self._audit],
                "audit_head": self._audit[-1].audit_hash if self._audit else "",
                # A bounded deque may discard an older prefix.  The retained
                # first entry still pins that prefix through previous_hash;
                # expose this fact so restore does not mistake a truncated
                # chain for a forged genesis record.
                "audit_truncated": bool(self._audit and self._audit[0].previous_hash),
            }

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any],
        *,
        approval_validator: ApprovalValidator | None = None,
        expected_root: str | os.PathLike[str] | None = None,
    ) -> "ControlledEnvironment":
        """Restore policy/audit metadata; an approval validator is never serialized."""
        if not isinstance(data, Mapping):
            raise ValueError("environment snapshot must be a mapping")
        if data.get("schema_version", ENVIRONMENT_SCHEMA_VERSION) != ENVIRONMENT_SCHEMA_VERSION:
            raise ValueError("unsupported environment schema")
        env = cls(
            kind=data.get("kind", EnvironmentKind.EVALUATION.value),
            root=data.get("root", "."),
            network_enabled=bool(data.get("network_enabled", False)),
            approval_validator=approval_validator,
            audit_limit=data.get("audit_limit", 1_024),
            expected_root=expected_root,
        )
        supplied_root_fp = str(data.get("root_fingerprint", "")).strip().lower()
        if len(supplied_root_fp) != 64 or supplied_root_fp != _sha256({"root": str(env.root)}):
            raise ValueError("environment root fingerprint mismatch")
        supplied_limit_fp = str(data.get("audit_limit_fingerprint", "")).strip().lower()
        if len(supplied_limit_fp) != 64 or supplied_limit_fp != _sha256({"audit_limit": env.audit_limit}):
            raise ValueError("environment audit limit fingerprint mismatch")
        raw_audit = data.get("audit", [])
        if not isinstance(raw_audit, list) or len(raw_audit) > env.audit_limit:
            raise ValueError("environment audit is not bounded")
        truncated = bool(data.get("audit_truncated", False))
        for raw in raw_audit:
            if not isinstance(raw, Mapping):
                raise ValueError("invalid environment audit entry")
            required = {"request_target", "request_read_only", "request_uses_network", "previous_hash", "audit_hash"}
            if not required.issubset(raw):
                raise ValueError("environment audit entry lacks integrity fields")
            entry = EnvironmentAudit(
                request_hash=raw.get("request_hash", ""),
                allowed=raw.get("allowed", False),
                reason=raw.get("reason", ""),
                environment=raw.get("environment", env.kind.value),
                action=raw.get("action", ""),
                target_scope=raw.get("target_scope", ""),
                timestamp=raw.get("timestamp", _utc_now()),
                request_target=raw.get("request_target", ""),
                request_read_only=raw.get("request_read_only", True),
                request_uses_network=raw.get("request_uses_network", False),
                previous_hash=raw.get("previous_hash", ""),
                audit_hash=raw.get("audit_hash", ""),
            )
            if entry.environment is not env.kind:
                raise ValueError("environment audit kind mismatch")
            expected_previous = (
                entry.previous_hash
                if not env._audit and truncated
                else (env._audit[-1].audit_hash if env._audit else "")
            )
            if not entry.verify(expected_previous):
                raise ValueError("environment audit chain is invalid")
            env._audit.append(entry)
        supplied_head = str(data.get("audit_head", "")).strip().lower()
        if supplied_head != (env._audit[-1].audit_hash if env._audit else ""):
            raise ValueError("environment audit head mismatch")
        return env


__all__ = [
    "HOMEOSTASIS_SCHEMA_VERSION",
    "RESOURCE_LEDGER_SCHEMA_VERSION",
    "ENVIRONMENT_SCHEMA_VERSION",
    "ADDITIVE_RESOURCES",
    "GAUGE_RESOURCES",
    "KNOWN_RESOURCES",
    "HomeostasisAction",
    "ResourceLedgerCapacityError",
    "EnvironmentKind",
    "ResourceBudget",
    "ResourceObservation",
    "HomeostasisDecision",
    "ResourceEvent",
    "ResourceLedger",
    "HomeostasisController",
    "ActionRequest",
    "ActionDecision",
    "EnvironmentAudit",
    "ControlledEnvironment",
]
