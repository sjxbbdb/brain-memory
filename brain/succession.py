"""Bounded continuity anchors and disaster-only succession.

This module is intentionally a policy/data seam.  It does not start a
successor, execute code, copy a process, or grant access to an environment.
It records which evidence is trusted, applies the inheritance boundary from
``docs/adr/0003-succession-and-inheritance.md``, and produces an immutable
``SuccessionRecord`` that a later lifecycle coordinator may consume.

The important distinction is between *continuity* and *copying state*:

* constitutional anchors (identity root, purpose, life rules and lineage
  metadata) are required and may cross a generation boundary;
* verified history/memory may cross with provenance;
* skills and strategies are held for independent re-evaluation;
* credentials, approvals, sessions, active work, transient affect and
  unconfirmed actions never cross, even when a caller labels them verified.

All records are bounded, frozen after construction, JSON round-trippable and
content-hash pinned.  The in-memory vault is append-only; persistence belongs
to a caller-controlled adapter, just as it does for :mod:`brain.life_kernel`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping
import uuid


SUCCESSION_SCHEMA_VERSION = 1
ANCHOR_SCHEMA_VERSION = 1
VAULT_SCHEMA_VERSION = 1
SUCCESSION_RECORD_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Small, deliberately conservative serialization helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 500, default: str = "") -> str:
    if value is None:
        return default
    try:
        result = str(value)
    except Exception:
        return default
    # Keep non-ASCII text, but remove NUL/control characters that make audit
    # records ambiguous.  Newline/tab are retained for human-readable reason
    # fields and are still JSON escaped at serialization time.
    result = "".join(ch if ord(ch) >= 32 or ch in "\r\n\t" else " " for ch in result)
    return result[: max(0, int(limit))]


def _bounded_int(value: Any, *, name: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool):
        raise SuccessionError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SuccessionError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise SuccessionError(f"{name} outside allowed range")
    return result


def _finite_float(value: Any, *, name: str, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if not math.isfinite(result):
        result = default
    return result


def _bool(value: Any, default: bool = False) -> bool:
    """Parse a bounded boolean without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    raw = _text(value, 20).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "accepted", "pass", "passed"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "rejected", "fail", "failed"}:
        return False
    return bool(default)


def _safe_json(value: Any, *, depth: int = 0, max_depth: int = 5) -> Any:
    """Bound arbitrary input before it enters a hash or an audit record."""

    if depth > max_depth:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, str):
        return _text(value, 1000)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:64]:
            key = _text(raw_key, 120).strip()
            if key:
                result[key] = _safe_json(raw_value, depth=depth + 1, max_depth=max_depth)
        return result
    if isinstance(value, (set, frozenset)):
        iterable = sorted(value, key=lambda item: repr(item))
    elif isinstance(value, (list, tuple)):
        iterable = value
    else:
        iterable = ()
    if iterable:
        return [
            _safe_json(item, depth=depth + 1, max_depth=max_depth)
            for item in list(iterable)[:64]
        ]
    if isinstance(value, (list, tuple, set, frozenset)):
        return []
    # An arbitrary object is evidence metadata, not executable content.
    return _text(value, 500)


def _freeze(value: Any, *, depth: int = 0) -> Any:
    """Recursively freeze bounded JSON-like data."""

    if depth > 5:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        return MappingProxyType({
            _text(key, 120): _freeze(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
            if _text(key, 120)
        })
    if isinstance(value, (set, frozenset)):
        iterable = sorted(value, key=lambda item: repr(item))
    elif isinstance(value, (list, tuple)):
        iterable = value
    else:
        iterable = ()
    if iterable:
        return tuple(_freeze(item, depth=depth + 1) for item in list(iterable)[:64])
    if isinstance(value, (list, tuple, set, frozenset)):
        return ()
    if isinstance(value, float) and not math.isfinite(value):
        return 0.0
    if isinstance(value, str):
        return _text(value, 1000)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_strings(value: Any, *, limit: int = 32, item_limit: int = 240) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in list(value)[:limit]:
        item_text = _text(item, item_limit).strip()
        if item_text and item_text not in seen:
            seen.add(item_text)
            result.append(item_text)
    return tuple(result)


# ---------------------------------------------------------------------------
# Errors and enums
# ---------------------------------------------------------------------------


class SuccessionError(RuntimeError):
    """Base class for a rejected or unverifiable continuity operation."""


class AnchorIntegrityError(SuccessionError):
    """An anchor, set or vault entry is malformed or has been rewritten."""


class SuccessionPolicyError(SuccessionError):
    """A policy boundary (for example, copying a credential) was violated."""


class SuccessionRecordError(SuccessionError):
    """A succession record cannot be created or verified."""


# Friendly aliases used by embedders and early design notes.
AnchorValidationError = AnchorIntegrityError
InheritanceError = SuccessionPolicyError


class AnchorKind(str, Enum):
    """The evidence class of an anchor.

    Values are lower-case stable wire names.  Several aliases are retained
    because early prototypes used ``identity_core``/``purpose`` and callers
    should not need a migration merely to read a record.
    """

    IDENTITY_ROOT = "identity_root"
    IDENTITY_CORE = "identity_root"  # alias
    IDENTITY = "identity_root"  # alias
    CORE_PURPOSE = "core_purpose"
    PURPOSE = "core_purpose"  # alias
    LIFE_RULE = "life_rule"
    LIFE_RULES = "life_rule"  # alias
    LINEAGE_METADATA = "lineage_metadata"
    LINEAGE = "lineage_metadata"  # alias
    LIFE_HISTORY = "life_history"
    HISTORY = "life_history"  # alias
    MEMORY = "memory"
    SKILL = "skill"
    STRATEGY = "strategy"
    AFFECT_STATE = "affect_state"
    TRANSIENT_AFFECT = "affect_state"  # alias
    TRANSIENT_EMOTION = "affect_state"  # alias
    EMOTION = "affect_state"  # alias
    ACTIVE_TASK = "active_task"
    TASK = "active_task"  # alias
    UNCONFIRMED_ACTION = "unconfirmed_action"
    ACTION = "unconfirmed_action"  # alias
    CREDENTIAL = "credential"
    CREDENTIALS = "credential"  # alias
    APPROVAL_TOKEN = "approval_token"
    APPROVAL = "approval_token"  # alias
    TOKEN = "approval_token"  # alias
    EXTERNAL_SESSION = "external_session"
    SESSION = "external_session"  # alias
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: "AnchorKind | str") -> "AnchorKind":
        if isinstance(value, cls):
            return value
        raw = _text(value, 100).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "identityroot": "identity_root",
            "identity_core": "identity_root",
            "identity": "identity_root",
            "purpose": "core_purpose",
            "corepurpose": "core_purpose",
            "life_rules": "life_rule",
            "liferule": "life_rule",
            "lineage": "lineage_metadata",
            "lineage_meta": "lineage_metadata",
            "history": "life_history",
            "narrative": "life_history",
            "verified_history": "life_history",
            "affect": "affect_state",
            "emotion": "affect_state",
            "transient_emotion": "affect_state",
            "task": "active_task",
            "active_work": "active_task",
            "action": "unconfirmed_action",
            "unconfirmed": "unconfirmed_action",
            "credential": "credential",
            "credentials": "credential",
            "approval": "approval_token",
            "approval_token": "approval_token",
            "external_session": "external_session",
            "session": "external_session",
            "身份根": "identity_root",
            "身份核": "identity_root",
            "核心目的": "core_purpose",
            "生命规则": "life_rule",
            "谱系元数据": "lineage_metadata",
            "生命史": "life_history",
            "技能": "skill",
            "策略": "strategy",
            "瞬时情绪": "affect_state",
            "活动任务": "active_task",
            "未确认行动": "unconfirmed_action",
            "凭证": "credential",
            "审批令牌": "approval_token",
            "外部会话": "external_session",
        }
        raw = aliases.get(raw, raw)
        try:
            return cls(raw)
        except ValueError as exc:
            raise AnchorIntegrityError(f"unknown anchor kind: {value!r}") from exc


class AnchorTrust(str, Enum):
    """Evidence trust tier, ordered from constitutional to untrusted."""

    CONSTITUTIONAL = "constitutional"
    VERIFIED = "verified"
    REEVALUATED = "reevaluated"
    EVALUATED = "reevaluated"  # alias
    UNVERIFIED = "unverified"
    QUARANTINED = "quarantined"
    TRUSTED = "verified"  # alias

    @classmethod
    def parse(cls, value: "AnchorTrust | str") -> "AnchorTrust":
        if isinstance(value, cls):
            return value
        raw = _text(value, 80).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "constitutional": "constitutional",
            "core": "constitutional",
            "root": "constitutional",
            "trusted": "verified",
            "verified": "verified",
            "proven": "verified",
            "reevaluated": "reevaluated",
            "re_evaluated": "reevaluated",
            "evaluated": "reevaluated",
            "unverified": "unverified",
            "unknown": "unverified",
            "quarantined": "quarantined",
            "isolated": "quarantined",
        }
        raw = aliases.get(raw, raw)
        try:
            return cls(raw)
        except ValueError as exc:
            raise AnchorIntegrityError(f"unknown anchor trust: {value!r}") from exc


# The name used in some design notes.
TrustLevel = AnchorTrust


class FailureClass(str, Enum):
    """Coarse incident classes used to choose recovery or succession."""

    TRANSIENT = "TRANSIENT"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    CAPABILITY_DEGRADATION = "CAPABILITY_FAILURE"  # alias
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    HARD_INTEGRITY_FAILURE = "HARD_INTEGRITY_FAILURE"
    INTEGRITY_FAILURE = "HARD_INTEGRITY_FAILURE"  # alias
    LEDGER_INTEGRITY = "HARD_INTEGRITY_FAILURE"  # alias
    DATA_CORRUPTION = "HARD_INTEGRITY_FAILURE"  # alias
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    METACOGNITIVE_DRIFT = "METACOGNITIVE_DRIFT"
    SECURITY_BREACH = "SECURITY_BREACH"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: "FailureClass | str") -> "FailureClass":
        if isinstance(value, cls):
            return value
        raw = _text(value, 120).strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "TEMPORARY": "TRANSIENT",
            "TIMEOUT": "TRANSIENT",
            "RESOURCE": "RESOURCE_EXHAUSTION",
            "OOM": "RESOURCE_EXHAUSTION",
            "CAPABILITY_DEGRADATION": "CAPABILITY_FAILURE",
            "DEGRADED": "CAPABILITY_FAILURE",
            "ENVIRONMENT": "ENVIRONMENT_FAILURE",
            "INTEGRITY": "HARD_INTEGRITY_FAILURE",
            "HARD_INTEGRITY": "HARD_INTEGRITY_FAILURE",
            "LEDGER_CORRUPTION": "HARD_INTEGRITY_FAILURE",
            "IDENTITY_MISALIGNMENT": "IDENTITY_DRIFT",
            "META_DRIFT": "METACOGNITIVE_DRIFT",
            "METACOGNITION_DRIFT": "METACOGNITIVE_DRIFT",
            "SECURITY": "SECURITY_BREACH",
            "UNKNOWN_FAILURE": "UNKNOWN",
            "临时故障": "TRANSIENT",
            "资源耗尽": "RESOURCE_EXHAUSTION",
            "能力故障": "CAPABILITY_FAILURE",
            "环境故障": "ENVIRONMENT_FAILURE",
            "硬完整性故障": "HARD_INTEGRITY_FAILURE",
            "身份漂移": "IDENTITY_DRIFT",
            "元认知漂移": "METACOGNITIVE_DRIFT",
            "安全故障": "SECURITY_BREACH",
        }
        raw = aliases.get(raw, raw)
        try:
            return cls(raw)
        except ValueError as exc:
            raise SuccessionError(f"unknown failure class: {value!r}") from exc


FailureCategory = FailureClass


class FailureDisposition(str, Enum):
    """Conservative next boundary; this is not an execution command."""

    RECOVERY = "RECOVERY"
    QUARANTINE = "QUARANTINE"
    SUCCESSION = "SUCCESSION"
    RETIRE = "RETIRE"


class InheritanceDisposition(str, Enum):
    INHERITED = "INHERITED"
    INHERITED_AFTER_REEVALUATION = "INHERITED_AFTER_REEVALUATION"
    REEVALUATION_REQUIRED = "REEVALUATION_REQUIRED"
    EXCLUDED = "EXCLUDED"
    QUARANTINED = "QUARANTINED"


# ---------------------------------------------------------------------------
# Failure assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureAssessment:
    """Immutable evidence summary for one incident.

    A drift label alone never authorizes a new generation.  Identity and
    metacognitive drift require explicit confirmation *and* a failed recovery
    attempt.  A hard integrity/security failure is treated as a succession
    candidate immediately because continuing with potentially rewritten
    constitutional state is unsafe; the lifecycle coordinator still decides
    whether to retire instead.
    """

    failure_class: FailureClass | str
    reason: str
    evidence_refs: tuple[str, ...] = ()
    confirmed: bool = False
    recovery_attempted: bool = False
    recovery_failed: bool = False
    incident_id: str = field(default_factory=lambda: f"incident-{uuid.uuid4().hex}")
    detected_at: str = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    assessment_hash: str = ""

    def __post_init__(self) -> None:
        failure_class = FailureClass.parse(self.failure_class)
        reason = _text(self.reason, 1000).strip() or "unspecified incident"
        evidence = _bounded_strings(self.evidence_refs, limit=32, item_limit=300)
        confirmed = _bool(self.confirmed)
        attempted = _bool(self.recovery_attempted) or _bool(self.recovery_failed)
        failed = _bool(self.recovery_failed)
        incident_id = _text(self.incident_id, 160).strip()
        if not incident_id:
            raise SuccessionError("incident_id must not be empty")
        detected_at = _text(self.detected_at, 100).strip() or _utc_now()
        metadata = _freeze(_safe_json(self.metadata))
        object.__setattr__(self, "failure_class", failure_class)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "confirmed", confirmed)
        object.__setattr__(self, "recovery_attempted", attempted)
        object.__setattr__(self, "recovery_failed", failed)
        object.__setattr__(self, "incident_id", incident_id)
        object.__setattr__(self, "detected_at", detected_at)
        object.__setattr__(self, "metadata", metadata)
        expected = self._compute_hash()
        supplied = _text(self.assessment_hash, 128).strip()
        if supplied and supplied != expected:
            raise AnchorIntegrityError("failure assessment hash mismatch")
        object.__setattr__(self, "assessment_hash", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "confirmed": self.confirmed,
            "recovery_attempted": self.recovery_attempted,
            "recovery_failed": self.recovery_failed,
            "incident_id": self.incident_id,
            "detected_at": self.detected_at,
            "metadata": _thaw(self.metadata),
        }

    def _compute_hash(self) -> str:
        return _sha(self._payload())

    @property
    def fingerprint(self) -> str:
        return self.assessment_hash

    @property
    def requires_succession(self) -> bool:
        if self.failure_class in {
            FailureClass.HARD_INTEGRITY_FAILURE,
            FailureClass.SECURITY_BREACH,
        }:
            return True
        if self.failure_class in {
            FailureClass.IDENTITY_DRIFT,
            FailureClass.METACOGNITIVE_DRIFT,
        }:
            return self.confirmed and self.recovery_attempted and self.recovery_failed
        return False

    @property
    def succession_eligible(self) -> bool:
        return self.requires_succession

    @property
    def disposition(self) -> FailureDisposition:
        if self.requires_succession:
            return FailureDisposition.SUCCESSION
        if self.failure_class in {
            FailureClass.IDENTITY_DRIFT,
            FailureClass.METACOGNITIVE_DRIFT,
            FailureClass.UNKNOWN,
        }:
            return FailureDisposition.QUARANTINE
        return FailureDisposition.RECOVERY

    @property
    def recovery_failed_after_confirmation(self) -> bool:
        return bool(self.confirmed and self.recovery_attempted and self.recovery_failed)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["schema_version"] = SUCCESSION_SCHEMA_VERSION
        payload["assessment_hash"] = self.assessment_hash
        return payload

    def verify(self) -> bool:
        if self._compute_hash() != self.assessment_hash:
            raise AnchorIntegrityError("failure assessment hash mismatch")
        return True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "FailureAssessment":
        if not isinstance(data, Mapping):
            raise AnchorIntegrityError("failure assessment must be a mapping")
        return cls(
            failure_class=data.get("failure_class", data.get("category", FailureClass.UNKNOWN.value)),
            reason=data.get("reason", "unspecified incident"),
            evidence_refs=data.get("evidence_refs", data.get("evidence", ())),
            confirmed=data.get("confirmed", False),
            recovery_attempted=data.get("recovery_attempted", False),
            recovery_failed=data.get("recovery_failed", False),
            incident_id=data.get("incident_id", ""),
            detected_at=data.get("detected_at", _utc_now()),
            metadata=data.get("metadata", {}),
            assessment_hash=data.get("assessment_hash", data.get("integrity_hash", "")),
        )


def classify_failure(
    failure_class: FailureClass | str,
    reason: str = "unspecified incident",
    **kwargs: Any,
) -> FailureAssessment:
    """Build a normalized assessment from a class label and evidence.

    This helper deliberately returns a record rather than a command.  A
    caller must still freeze the parent and pass an independent evaluation
    gate before invoking a lifecycle transition.
    """

    return FailureAssessment(failure_class=failure_class, reason=reason, **kwargs)


class FailureClassifier:
    """Compatibility façade for code preferring an object-style classifier."""

    @staticmethod
    def classify(failure_class: FailureClass | str, reason: str = "unspecified incident", **kwargs: Any) -> FailureAssessment:
        return classify_failure(failure_class, reason, **kwargs)


# ---------------------------------------------------------------------------
# Anchors and immutable anchor sets
# ---------------------------------------------------------------------------


_CONSTITUTIONAL_KINDS = frozenset({
    AnchorKind.IDENTITY_ROOT,
    AnchorKind.CORE_PURPOSE,
    AnchorKind.LIFE_RULE,
    AnchorKind.LINEAGE_METADATA,
})
_HISTORY_KINDS = frozenset({AnchorKind.LIFE_HISTORY, AnchorKind.MEMORY})
_RE_EVALUATE_KINDS = frozenset({AnchorKind.SKILL, AnchorKind.STRATEGY})
_NEVER_INHERIT_KINDS = frozenset({
    AnchorKind.AFFECT_STATE,
    AnchorKind.ACTIVE_TASK,
    AnchorKind.UNCONFIRMED_ACTION,
    AnchorKind.CREDENTIAL,
    AnchorKind.APPROVAL_TOKEN,
    AnchorKind.EXTERNAL_SESSION,
})


@dataclass(frozen=True, slots=True)
class Anchor:
    """One provenance-bearing, content-hash-pinned continuity anchor."""

    kind: AnchorKind | str
    value: Any
    source: str = "unknown"
    verified: bool = False
    trust_level: AnchorTrust | str = AnchorTrust.UNVERIFIED
    evidence_refs: tuple[str, ...] = ()
    anchor_id: str = field(default_factory=lambda: f"anchor-{uuid.uuid4().hex}")
    created_at: str = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    integrity_hash: str = ""

    def __post_init__(self) -> None:
        kind = AnchorKind.parse(self.kind)
        anchor_id = _text(self.anchor_id, 160).strip()
        if not anchor_id:
            raise AnchorIntegrityError("anchor_id must not be empty")
        source = _text(self.source, 300).strip() or "unknown"
        verified = _bool(self.verified)
        trust = AnchorTrust.parse(self.trust_level)
        # A caller may provide only ``verified=True``; normalize that common
        # form to the ordinary verified tier.  Constitutional trust remains an
        # explicit claim and is checked by AnchorSet/vault policy.
        if verified and trust == AnchorTrust.UNVERIFIED:
            trust = AnchorTrust.VERIFIED
        if not verified and trust in {AnchorTrust.CONSTITUTIONAL, AnchorTrust.VERIFIED, AnchorTrust.REEVALUATED}:
            # Never let an unverified record masquerade as trusted merely by
            # setting a label in a JSON snapshot.
            trust = AnchorTrust.UNVERIFIED
        if trust == AnchorTrust.REEVALUATED and not verified:
            raise AnchorIntegrityError("reevaluated anchor must be verified")
        evidence = _bounded_strings(self.evidence_refs, limit=32, item_limit=300)
        created_at = _text(self.created_at, 100).strip() or _utc_now()
        value = _freeze(_safe_json(self.value))
        metadata = _freeze(_safe_json(self.metadata))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "verified", verified)
        object.__setattr__(self, "trust_level", trust)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "anchor_id", anchor_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "metadata", metadata)
        expected = self._compute_hash()
        supplied = _text(self.integrity_hash, 128).strip()
        if supplied and supplied != expected:
            raise AnchorIntegrityError("anchor integrity hash mismatch")
        object.__setattr__(self, "integrity_hash", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "value": _thaw(self.value),
            "source": self.source,
            "verified": self.verified,
            "trust_level": self.trust_level.value,
            "evidence_refs": list(self.evidence_refs),
            "anchor_id": self.anchor_id,
            "created_at": self.created_at,
            "metadata": _thaw(self.metadata),
        }

    def _compute_hash(self) -> str:
        return _sha(self._payload())

    @property
    def content_hash(self) -> str:
        return self.integrity_hash

    @property
    def fingerprint(self) -> str:
        return self.integrity_hash

    @property
    def anchor_type(self) -> AnchorKind:
        return self.kind

    @property
    def category(self) -> AnchorKind:
        return self.kind

    @property
    def payload(self) -> Any:
        return self.value

    @property
    def trust(self) -> AnchorTrust:
        return self.trust_level

    @property
    def traceable(self) -> bool:
        return bool(
            self.verified
            and self.trust_level not in {AnchorTrust.UNVERIFIED, AnchorTrust.QUARANTINED}
            and self.source.strip().lower() not in {"", "unknown", "untrusted"}
            and self.evidence_refs
        )

    def verify(self) -> bool:
        if self._compute_hash() != self.integrity_hash:
            raise AnchorIntegrityError("anchor integrity hash mismatch")
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload.update({
            "schema_version": ANCHOR_SCHEMA_VERSION,
            "integrity_hash": self.integrity_hash,
            # Stable aliases help old consumers while the canonical field is
            # still ``kind``.
            "anchor_type": self.kind.value,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Anchor":
        if not isinstance(data, Mapping):
            raise AnchorIntegrityError("anchor must be a mapping")
        return cls(
            kind=data.get("kind", data.get("anchor_type", data.get("category", "unknown"))),
            value=data.get("value", data.get("payload")),
            source=data.get("source", "unknown"),
            verified=data.get("verified", data.get("is_verified", False)),
            trust_level=data.get("trust_level", data.get("trust", AnchorTrust.UNVERIFIED.value)),
            evidence_refs=data.get("evidence_refs", data.get("evidence", ())),
            anchor_id=data.get("anchor_id", data.get("id", "")),
            created_at=data.get("created_at", _utc_now()),
            metadata=data.get("metadata", {}),
            integrity_hash=data.get("integrity_hash", data.get("content_hash", data.get("hash", ""))),
        )


@dataclass(frozen=True, slots=True)
class AnchorSet:
    """A sealed view of anchors belonging to one concrete instance."""

    lineage_id: str
    generation: int
    instance_id: str
    anchors: tuple[Anchor | Mapping[str, Any], ...] = ()
    anchor_set_id: str = field(default_factory=lambda: f"anchor-set-{uuid.uuid4().hex}")
    created_at: str = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    set_hash: str = ""

    def __post_init__(self) -> None:
        lineage = _text(self.lineage_id, 160).strip()
        instance = _text(self.instance_id, 160).strip()
        if not lineage or not instance:
            raise AnchorIntegrityError("lineage_id and instance_id must not be empty")
        generation = _bounded_int(self.generation, name="generation")
        anchor_set_id = _text(self.anchor_set_id, 180).strip()
        if not anchor_set_id:
            raise AnchorIntegrityError("anchor_set_id must not be empty")
        parsed: list[Anchor] = []
        seen: set[str] = set()
        for raw in list(self.anchors)[:512]:
            anchor = raw if isinstance(raw, Anchor) else Anchor.from_dict(raw)
            anchor.verify()
            if anchor.anchor_id in seen:
                raise AnchorIntegrityError(f"duplicate anchor_id: {anchor.anchor_id}")
            seen.add(anchor.anchor_id)
            parsed.append(anchor)
        created_at = _text(self.created_at, 100).strip() or _utc_now()
        metadata = _freeze(_safe_json(self.metadata))
        object.__setattr__(self, "lineage_id", lineage)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "instance_id", instance)
        object.__setattr__(self, "anchors", tuple(parsed))
        object.__setattr__(self, "anchor_set_id", anchor_set_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "metadata", metadata)
        expected = self._compute_hash()
        supplied = _text(self.set_hash, 128).strip()
        if supplied and supplied != expected:
            raise AnchorIntegrityError("anchor set hash mismatch")
        object.__setattr__(self, "set_hash", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "generation": self.generation,
            "instance_id": self.instance_id,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "anchor_set_id": self.anchor_set_id,
            "created_at": self.created_at,
            "metadata": _thaw(self.metadata),
        }

    def _compute_hash(self) -> str:
        return _sha(self._payload())

    @property
    def anchor_set_hash(self) -> str:
        return self.set_hash

    @property
    def integrity_hash(self) -> str:
        return self.set_hash

    @property
    def fingerprint(self) -> str:
        return self.set_hash

    def __len__(self) -> int:
        return len(self.anchors)

    def __iter__(self):
        return iter(self.anchors)

    @classmethod
    def create(
        cls,
        *,
        lineage_id: str,
        generation: int,
        instance_id: str,
        anchors: Iterable[Anchor | Mapping[str, Any]],
        anchor_set_id: str | None = None,
        created_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "AnchorSet":
        return cls(
            lineage_id=lineage_id,
            generation=generation,
            instance_id=instance_id,
            anchors=tuple(anchors),
            anchor_set_id=anchor_set_id or f"anchor-set-{uuid.uuid4().hex}",
            created_at=created_at or _utc_now(),
            metadata=metadata or {},
        )

    def by_kind(self, kind: AnchorKind | str) -> tuple[Anchor, ...]:
        wanted = AnchorKind.parse(kind)
        return tuple(anchor for anchor in self.anchors if anchor.kind == wanted)

    def get(self, anchor_id: str) -> Anchor | None:
        wanted = str(anchor_id)
        return next((anchor for anchor in self.anchors if anchor.anchor_id == wanted), None)

    @property
    def constitutional_anchors(self) -> tuple[Anchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.kind in _CONSTITUTIONAL_KINDS)

    @property
    def identity_anchors(self) -> tuple[Anchor, ...]:
        return self.by_kind(AnchorKind.IDENTITY_ROOT)

    @property
    def purpose_anchors(self) -> tuple[Anchor, ...]:
        return self.by_kind(AnchorKind.CORE_PURPOSE)

    @property
    def life_rule_anchors(self) -> tuple[Anchor, ...]:
        return self.by_kind(AnchorKind.LIFE_RULE)

    @property
    def lineage_anchors(self) -> tuple[Anchor, ...]:
        return self.by_kind(AnchorKind.LINEAGE_METADATA)

    @property
    def history_anchors(self) -> tuple[Anchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.kind in _HISTORY_KINDS)

    @property
    def capability_anchors(self) -> tuple[Anchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.kind in _RE_EVALUATE_KINDS)

    @property
    def missing_constitutional_kinds(self) -> frozenset[AnchorKind]:
        present = {anchor.kind for anchor in self.anchors}
        return frozenset(_CONSTITUTIONAL_KINDS - present)

    def constitutional_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for kind in _CONSTITUTIONAL_KINDS:
            candidates = [anchor for anchor in self.anchors if anchor.kind == kind]
            if not candidates:
                errors.append(f"missing:{kind.value}")
                continue
            if not any(
                anchor.verified
                and anchor.trust_level == AnchorTrust.CONSTITUTIONAL
                and anchor.traceable
                for anchor in candidates
            ):
                errors.append(f"untrusted:{kind.value}")
        # If a root payload explicitly names a lineage, it must agree with the
        # set owner.  Opaque values remain valid because older snapshots used a
        # string rather than a mapping.
        for anchor in self.by_kind(AnchorKind.IDENTITY_ROOT):
            value = _thaw(anchor.value)
            if isinstance(value, Mapping) and value.get("lineage_id") not in (None, self.lineage_id):
                errors.append("identity_root:lineage_mismatch")
        for anchor in self.by_kind(AnchorKind.LINEAGE_METADATA):
            value = _thaw(anchor.value)
            if isinstance(value, Mapping) and value.get("lineage_id") not in (None, self.lineage_id):
                errors.append("lineage_metadata:lineage_mismatch")
        return tuple(errors)

    def validate_for_succession(self) -> bool:
        errors = self.constitutional_errors()
        if errors:
            raise AnchorIntegrityError("anchor set is not succession-ready: " + ", ".join(errors))
        return True

    def validate_for_vault(self) -> bool:
        self.validate_for_succession()
        forbidden = [
            anchor.anchor_id
            for anchor in self.anchors
            if anchor.kind in _NEVER_INHERIT_KINDS
        ]
        if forbidden:
            raise AnchorIntegrityError(
                "vault refuses non-continuity anchors: " + ", ".join(forbidden)
            )
        untraceable = [anchor.anchor_id for anchor in self.anchors if not anchor.traceable]
        if untraceable:
            raise AnchorIntegrityError(
                "vault requires verified traceable anchors: " + ", ".join(untraceable)
            )
        return True

    def filter_for_successor(
        self,
        *,
        successor_generation: int | None = None,
        reevaluated_anchor_ids: Iterable[str] | None = None,
        evaluation_receipts: Mapping[str, Any] | None = None,
    ) -> "InheritancePlan":
        """Convenience façade for the module-level inheritance filter."""
        return filter_inheritance(
            self,
            successor_generation=successor_generation,
            reevaluated_anchor_ids=reevaluated_anchor_ids,
            evaluation_receipts=evaluation_receipts,
        )

    def verify(self) -> bool:
        for anchor in self.anchors:
            anchor.verify()
        if self._compute_hash() != self.set_hash:
            raise AnchorIntegrityError("anchor set hash mismatch")
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload.update({
            "schema_version": ANCHOR_SCHEMA_VERSION,
            "set_hash": self.set_hash,
            "anchor_set_hash": self.set_hash,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AnchorSet":
        if not isinstance(data, Mapping):
            raise AnchorIntegrityError("anchor set must be a mapping")
        raw_anchors = data.get("anchors", data.get("items", ()))
        if not isinstance(raw_anchors, (list, tuple)):
            raise AnchorIntegrityError("anchor set anchors must be a list")
        return cls(
            lineage_id=data.get("lineage_id", ""),
            generation=data.get("generation", 0),
            instance_id=data.get("instance_id", ""),
            anchors=tuple(raw_anchors),
            anchor_set_id=data.get("anchor_set_id", data.get("id", "")),
            created_at=data.get("created_at", _utc_now()),
            metadata=data.get("metadata", {}),
            set_hash=data.get("set_hash", data.get("anchor_set_hash", data.get("integrity_hash", ""))),
        )


# ---------------------------------------------------------------------------
# Append-only anchor vault
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnchorVaultEntry:
    """One append-only vault fact; the set itself remains independently hashed."""

    sequence: int
    anchor_set: AnchorSet
    previous_hash: str = ""
    entry_id: str = field(default_factory=lambda: f"vault-entry-{uuid.uuid4().hex}")
    created_at: str = field(default_factory=_utc_now)
    entry_hash: str = ""

    def __post_init__(self) -> None:
        sequence = _bounded_int(self.sequence, name="vault sequence", minimum=1)
        anchor_set = self.anchor_set if isinstance(self.anchor_set, AnchorSet) else AnchorSet.from_dict(self.anchor_set)
        anchor_set.verify()
        previous_hash = _text(self.previous_hash, 128).strip()
        entry_id = _text(self.entry_id, 180).strip()
        if not entry_id:
            raise AnchorIntegrityError("vault entry_id must not be empty")
        created_at = _text(self.created_at, 100).strip() or _utc_now()
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "anchor_set", anchor_set)
        object.__setattr__(self, "previous_hash", previous_hash)
        object.__setattr__(self, "entry_id", entry_id)
        object.__setattr__(self, "created_at", created_at)
        expected = _sha(self._payload())
        supplied = _text(self.entry_hash, 128).strip()
        if supplied and supplied != expected:
            raise AnchorIntegrityError("vault entry hash mismatch")
        object.__setattr__(self, "entry_hash", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "anchor_set": self.anchor_set.to_dict(),
            "previous_hash": self.previous_hash,
            "entry_id": self.entry_id,
            "created_at": self.created_at,
        }

    @property
    def fingerprint(self) -> str:
        return self.entry_hash

    def verify(self) -> bool:
        self.anchor_set.verify()
        if _sha(self._payload()) != self.entry_hash:
            raise AnchorIntegrityError("vault entry hash mismatch")
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload.update({"schema_version": VAULT_SCHEMA_VERSION, "entry_hash": self.entry_hash})
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AnchorVaultEntry":
        if not isinstance(data, Mapping):
            raise AnchorIntegrityError("vault entry must be a mapping")
        return cls(
            sequence=data.get("sequence", 0),
            anchor_set=AnchorSet.from_dict(data.get("anchor_set", {})),
            previous_hash=data.get("previous_hash", ""),
            entry_id=data.get("entry_id", ""),
            created_at=data.get("created_at", _utc_now()),
            entry_hash=data.get("entry_hash", data.get("integrity_hash", "")),
        )


class AnchorVault:
    """Thread-safe, append-only collection of sealed anchor sets.

    The vault is intentionally in-memory and has no delete/update API.  A
    host may persist ``snapshot()`` through its own controlled store and use
    ``from_snapshot`` on restart.  Every set must contain all four
    constitutional categories and must not contain secret/session material.
    """

    def __init__(
        self,
        anchor_sets: Iterable[AnchorSet | Mapping[str, Any]] | None = None,
        *,
        entries: Iterable[AnchorVaultEntry | Mapping[str, Any]] | None = None,
    ) -> None:
        self._lock = RLock()
        self._entries: tuple[AnchorVaultEntry, ...] = ()
        if entries is not None:
            for raw in entries:
                self._append_existing(raw if isinstance(raw, AnchorVaultEntry) else AnchorVaultEntry.from_dict(raw))
        elif anchor_sets is not None:
            for raw in anchor_sets:
                self.append(raw if isinstance(raw, AnchorSet) else AnchorSet.from_dict(raw))

    @property
    def entries(self) -> tuple[AnchorVaultEntry, ...]:
        return self._entries

    @property
    def anchor_sets(self) -> tuple[AnchorSet, ...]:
        return tuple(entry.anchor_set for entry in self._entries)

    @property
    def sets(self) -> tuple[AnchorSet, ...]:
        return self.anchor_sets

    @property
    def head(self) -> AnchorVaultEntry | None:
        return self._entries[-1] if self._entries else None

    @property
    def last_hash(self) -> str:
        return self.head.entry_hash if self.head else ""

    @property
    def last_sequence(self) -> int:
        return self.head.sequence if self.head else 0

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.anchor_sets)

    def _append_existing(self, entry: AnchorVaultEntry) -> AnchorVaultEntry:
        with self._lock:
            expected_sequence = len(self._entries) + 1
            if entry.sequence != expected_sequence:
                raise AnchorIntegrityError(
                    f"vault sequence gap: expected {expected_sequence}, got {entry.sequence}"
                )
            if entry.previous_hash != self.last_hash:
                raise AnchorIntegrityError("vault previous hash does not match head")
            entry.verify()
            # Re-run the vault policy when loading a pre-built entry or a
            # snapshot.  Hash validity alone is not permission to persist a
            # credential/session anchor; otherwise a caller could smuggle
            # secret material into a correctly signed-looking snapshot.
            entry.anchor_set.validate_for_vault()
            existing_ids = {item.anchor_set.anchor_set_id for item in self._entries}
            if entry.anchor_set.anchor_set_id in existing_ids:
                raise AnchorIntegrityError("anchor set has already been sealed")
            # One lineage may move forward only.  This prevents a stale parent
            # set from being appended after a successor has been sealed.
            for prior in self._entries:
                prior_set = prior.anchor_set
                current = entry.anchor_set
                if prior_set.lineage_id == current.lineage_id:
                    if current.generation <= prior_set.generation:
                        raise AnchorIntegrityError("anchor generation must increase within a lineage")
            object.__setattr__(self, "_entries", self._entries + (entry,))
            return entry

    def append(self, anchor_set: AnchorSet | Mapping[str, Any]) -> AnchorVaultEntry:
        parsed = anchor_set if isinstance(anchor_set, AnchorSet) else AnchorSet.from_dict(anchor_set)
        parsed.validate_for_vault()
        entry = AnchorVaultEntry(
            sequence=len(self._entries) + 1,
            anchor_set=parsed,
            previous_hash=self.last_hash,
        )
        return self._append_existing(entry)

    # Common persistence-oriented aliases.
    put = append
    store = append
    seal = append
    add = append
    append_set = append

    def get(self, anchor_set_id: str) -> AnchorSet | None:
        wanted = str(anchor_set_id)
        with self._lock:
            for entry in self._entries:
                if entry.anchor_set.anchor_set_id == wanted:
                    return entry.anchor_set
        return None

    def latest(
        self,
        lineage_id: str | None = None,
        *,
        instance_id: str | None = None,
    ) -> AnchorSet | None:
        lineage = None if lineage_id is None else str(lineage_id)
        instance = None if instance_id is None else str(instance_id)
        with self._lock:
            candidates = [
                entry.anchor_set
                for entry in self._entries
                if (lineage is None or entry.anchor_set.lineage_id == lineage)
                and (instance is None or entry.anchor_set.instance_id == instance)
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda item: (item.generation, item.created_at, item.anchor_set_id))

    latest_for = latest
    latest_set = latest

    def verify_chain(self) -> bool:
        probe = AnchorVault()
        for entry in self._entries:
            probe._append_existing(entry)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": VAULT_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self._entries],
            "last_sequence": self.last_sequence,
            "last_hash": self.last_hash,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any] | None) -> "AnchorVault":
        if not isinstance(data, Mapping):
            raise AnchorIntegrityError("anchor vault snapshot must be a mapping")
        raw_entries = data.get("entries", ())
        if not isinstance(raw_entries, (list, tuple)):
            raise AnchorIntegrityError("anchor vault entries must be a list")
        vault = cls(entries=[AnchorVaultEntry.from_dict(raw) for raw in raw_entries])
        if "last_sequence" in data and int(data.get("last_sequence", 0)) != vault.last_sequence:
            raise AnchorIntegrityError("anchor vault last_sequence mismatch")
        if "last_hash" in data and str(data.get("last_hash", "")) != vault.last_hash:
            raise AnchorIntegrityError("anchor vault last_hash mismatch")
        return vault


# ---------------------------------------------------------------------------
# Inheritance filter and plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InheritanceDecision:
    """A redacted decision; excluded values are intentionally not retained."""

    anchor_id: str
    kind: AnchorKind | str
    disposition: InheritanceDisposition | str
    reason: str
    source_hash: str
    evaluation_receipt_id: str | None = None

    def __post_init__(self) -> None:
        anchor_id = _text(self.anchor_id, 160).strip()
        if not anchor_id:
            raise SuccessionPolicyError("inheritance decision anchor_id must not be empty")
        kind = AnchorKind.parse(self.kind)
        try:
            disposition = self.disposition if isinstance(self.disposition, InheritanceDisposition) else InheritanceDisposition(str(self.disposition).strip().upper())
        except ValueError as exc:
            raise SuccessionPolicyError(f"unknown inheritance disposition: {self.disposition!r}") from exc
        source_hash = _text(self.source_hash, 128).strip()
        if len(source_hash) != 64:
            raise SuccessionPolicyError("inheritance decision source_hash is invalid")
        receipt = None if self.evaluation_receipt_id is None else _text(self.evaluation_receipt_id, 180).strip() or None
        object.__setattr__(self, "anchor_id", anchor_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "reason", _text(self.reason, 500).strip() or "unspecified")
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "evaluation_receipt_id", receipt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "kind": self.kind.value,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "source_hash": self.source_hash,
            "evaluation_receipt_id": self.evaluation_receipt_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "InheritanceDecision":
        if not isinstance(data, Mapping):
            raise SuccessionPolicyError("inheritance decision must be a mapping")
        return cls(
            anchor_id=data.get("anchor_id", data.get("id", "")),
            kind=data.get("kind", data.get("anchor_type", "unknown")),
            disposition=data.get("disposition", "EXCLUDED"),
            reason=data.get("reason", "unspecified"),
            source_hash=data.get("source_hash", ""),
            evaluation_receipt_id=data.get("evaluation_receipt_id"),
        )


@dataclass(frozen=True, slots=True)
class InheritancePlan:
    """Redacted, immutable result of applying the succession filter."""

    lineage_id: str
    parent_generation: int
    successor_generation: int
    source_anchor_set_id: str
    source_anchor_set_hash: str
    parent_instance_id: str
    inherited_anchors: tuple[Anchor, ...] = ()
    decisions: tuple[InheritanceDecision, ...] = ()
    # Skills and strategies that were present in the parent but did not yet
    # receive an independent evaluation.  They are retained as *candidates*
    # for an evaluator, never copied into the successor payload.
    reevaluation_anchors: tuple[Anchor, ...] = ()
    created_at: str = field(default_factory=_utc_now)
    plan_hash: str = ""

    def __post_init__(self) -> None:
        lineage = _text(self.lineage_id, 160).strip()
        parent_instance = _text(self.parent_instance_id, 160).strip()
        source_id = _text(self.source_anchor_set_id, 180).strip()
        source_hash = _text(self.source_anchor_set_hash, 128).strip()
        if not lineage or not parent_instance or not source_id or len(source_hash) != 64:
            raise SuccessionPolicyError("inheritance plan identity is incomplete")
        parent_generation = _bounded_int(self.parent_generation, name="parent_generation")
        successor_generation = _bounded_int(self.successor_generation, name="successor_generation")
        if successor_generation != parent_generation + 1:
            raise SuccessionPolicyError("successor generation must be parent generation + 1")
        inherited: list[Anchor] = []
        inherited_ids: set[str] = set()
        for raw in list(self.inherited_anchors)[:512]:
            anchor = raw if isinstance(raw, Anchor) else Anchor.from_dict(raw)
            anchor.verify()
            if anchor.anchor_id in inherited_ids:
                raise SuccessionPolicyError("duplicate inherited anchor")
            if anchor.kind in _NEVER_INHERIT_KINDS:
                raise SuccessionPolicyError(f"forbidden inherited anchor kind: {anchor.kind.value}")
            inherited_ids.add(anchor.anchor_id)
            inherited.append(anchor)
        pending: list[Anchor] = []
        pending_ids: set[str] = set()
        for raw in list(self.reevaluation_anchors)[:512]:
            anchor = raw if isinstance(raw, Anchor) else Anchor.from_dict(raw)
            anchor.verify()
            if anchor.kind not in _RE_EVALUATE_KINDS:
                raise SuccessionPolicyError(
                    "only skills/strategies may await re-evaluation"
                )
            if anchor.anchor_id in inherited_ids or anchor.anchor_id in pending_ids:
                raise SuccessionPolicyError("duplicate reevaluation anchor")
            pending_ids.add(anchor.anchor_id)
            pending.append(anchor)
        decisions: list[InheritanceDecision] = []
        decision_ids: set[str] = set()
        for raw in list(self.decisions)[:512]:
            decision = raw if isinstance(raw, InheritanceDecision) else InheritanceDecision.from_dict(raw)
            if decision.anchor_id in decision_ids:
                raise SuccessionPolicyError("duplicate inheritance decision")
            decision_ids.add(decision.anchor_id)
            decisions.append(decision)
        if inherited_ids - decision_ids:
            raise SuccessionPolicyError("inherited anchor has no decision")
        if any(
            decision.disposition in {
                InheritanceDisposition.INHERITED,
                InheritanceDisposition.INHERITED_AFTER_REEVALUATION,
            }
            and decision.anchor_id not in inherited_ids
            for decision in decisions
        ):
            raise SuccessionPolicyError("inheritance decision has no inherited anchor")
        decision_by_id = {decision.anchor_id: decision for decision in decisions}
        if pending_ids - decision_by_id.keys():
            raise SuccessionPolicyError("reevaluation anchor has no decision")
        if any(
            decision.disposition == InheritanceDisposition.REEVALUATION_REQUIRED
            and decision.anchor_id not in pending_ids
            for decision in decisions
        ):
            raise SuccessionPolicyError("reevaluation decision has no candidate anchor")
        created_at = _text(self.created_at, 100).strip() or _utc_now()
        object.__setattr__(self, "lineage_id", lineage)
        object.__setattr__(self, "parent_generation", parent_generation)
        object.__setattr__(self, "successor_generation", successor_generation)
        object.__setattr__(self, "source_anchor_set_id", source_id)
        object.__setattr__(self, "source_anchor_set_hash", source_hash)
        object.__setattr__(self, "parent_instance_id", parent_instance)
        object.__setattr__(self, "inherited_anchors", tuple(inherited))
        object.__setattr__(self, "decisions", tuple(decisions))
        object.__setattr__(self, "reevaluation_anchors", tuple(pending))
        object.__setattr__(self, "created_at", created_at)
        expected = self._compute_hash()
        supplied = _text(self.plan_hash, 128).strip()
        if supplied and supplied != expected:
            raise AnchorIntegrityError("inheritance plan hash mismatch")
        object.__setattr__(self, "plan_hash", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "parent_generation": self.parent_generation,
            "successor_generation": self.successor_generation,
            "source_anchor_set_id": self.source_anchor_set_id,
            "source_anchor_set_hash": self.source_anchor_set_hash,
            "parent_instance_id": self.parent_instance_id,
            "inherited_anchors": [anchor.to_dict() for anchor in self.inherited_anchors],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "reevaluation_anchors": [anchor.to_dict() for anchor in self.reevaluation_anchors],
            "created_at": self.created_at,
        }

    def _compute_hash(self) -> str:
        return _sha(self._payload())

    @property
    def fingerprint(self) -> str:
        return self.plan_hash

    @property
    def inherited(self) -> tuple[Anchor, ...]:
        return self.inherited_anchors

    @property
    def reevaluation_required(self) -> tuple[Anchor, ...]:
        return self.reevaluation_anchors

    @property
    def reevaluation_anchor_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.anchor_id
            for decision in self.decisions
            if decision.disposition == InheritanceDisposition.REEVALUATION_REQUIRED
        )

    @property
    def inherited_anchor_ids(self) -> tuple[str, ...]:
        return tuple(anchor.anchor_id for anchor in self.inherited_anchors)

    @property
    def excluded_anchor_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.anchor_id
            for decision in self.decisions
            if decision.disposition == InheritanceDisposition.EXCLUDED
        )

    @property
    def quarantined_anchor_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.anchor_id
            for decision in self.decisions
            if decision.disposition == InheritanceDisposition.QUARANTINED
        )

    @property
    def excluded_reasons(self) -> Mapping[str, str]:
        return MappingProxyType({
            decision.anchor_id: decision.reason
            for decision in self.decisions
            if decision.disposition in {
                InheritanceDisposition.EXCLUDED,
                InheritanceDisposition.QUARANTINED,
            }
        })

    @property
    def ready(self) -> bool:
        """Whether constitutional anchors are present in the copied payload."""
        inherited_kinds = {anchor.kind for anchor in self.inherited_anchors}
        return _CONSTITUTIONAL_KINDS <= inherited_kinds

    @property
    def is_ready(self) -> bool:
        return self.ready

    def decision_for(self, anchor_id: str) -> InheritanceDecision:
        wanted = str(anchor_id)
        for decision in self.decisions:
            if decision.anchor_id == wanted:
                return decision
        raise KeyError(wanted)

    def build_successor_anchor_set(
        self,
        instance_id: str,
        *,
        generation: int | None = None,
        anchor_set_id: str | None = None,
    ) -> AnchorSet:
        if not self.ready:
            raise SuccessionPolicyError("inheritance plan lacks constitutional anchors")
        target_generation = self.successor_generation if generation is None else int(generation)
        if target_generation != self.successor_generation:
            raise SuccessionPolicyError("successor generation does not match plan")
        return AnchorSet(
            lineage_id=self.lineage_id,
            generation=target_generation,
            instance_id=instance_id,
            anchors=self.inherited_anchors,
            anchor_set_id=anchor_set_id or f"anchor-set-g{target_generation}-{uuid.uuid4().hex[:12]}",
            metadata={
                "inherited_from": self.source_anchor_set_id,
                "inheritance_plan_hash": self.plan_hash,
            },
        )

    def verify(self) -> bool:
        for anchor in self.inherited_anchors:
            anchor.verify()
        if self._compute_hash() != self.plan_hash:
            raise AnchorIntegrityError("inheritance plan hash mismatch")
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload.update({
            "schema_version": SUCCESSION_SCHEMA_VERSION,
            "plan_hash": self.plan_hash,
            "inherited_anchor_ids": list(self.inherited_anchor_ids),
            "reevaluation_anchor_ids": list(self.reevaluation_anchor_ids),
            "excluded_anchor_ids": list(self.excluded_anchor_ids),
            "excluded_reasons": dict(self.excluded_reasons),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "InheritancePlan":
        if not isinstance(data, Mapping):
            raise SuccessionPolicyError("inheritance plan must be a mapping")
        raw_inherited = data.get("inherited_anchors", ())
        raw_decisions = data.get("decisions", ())
        raw_pending = data.get("reevaluation_anchors", data.get("pending_anchors", ()))
        if (
            not isinstance(raw_inherited, (list, tuple))
            or not isinstance(raw_decisions, (list, tuple))
            or not isinstance(raw_pending, (list, tuple))
        ):
            raise SuccessionPolicyError("inheritance plan collections must be lists")
        plan = cls(
            lineage_id=data.get("lineage_id", ""),
            parent_generation=data.get("parent_generation", 0),
            successor_generation=data.get("successor_generation", 1),
            source_anchor_set_id=data.get("source_anchor_set_id", ""),
            source_anchor_set_hash=data.get("source_anchor_set_hash", ""),
            parent_instance_id=data.get("parent_instance_id", ""),
            inherited_anchors=tuple(raw_inherited),
            decisions=tuple(raw_decisions),
            reevaluation_anchors=tuple(raw_pending),
            created_at=data.get("created_at", _utc_now()),
            plan_hash=data.get("plan_hash", data.get("integrity_hash", "")),
        )
        projection_checks = {
            "inherited_anchor_ids": list(plan.inherited_anchor_ids),
            "reevaluation_anchor_ids": list(plan.reevaluation_anchor_ids),
            "excluded_anchor_ids": list(plan.excluded_anchor_ids),
            "excluded_reasons": dict(plan.excluded_reasons),
        }
        for key, expected in projection_checks.items():
            if key in data and _safe_json(data.get(key)) != _safe_json(expected):
                raise SuccessionPolicyError(f"inheritance plan projection mismatch: {key}")
        return plan


def _evaluation_receipt_valid(receipt: Any) -> tuple[bool, str | None]:
    """Validate a caller-supplied independent evaluation receipt.

    The module does not trust arbitrary payloads as proof.  A receipt is
    accepted only when it explicitly says accepted/pass and, when it exposes a
    ``verify`` method, that method succeeds.  A bare ID in
    ``reevaluated_anchor_ids`` is treated as a host-attested reference and is
    recorded as ``external-attestation``; the later evaluator remains
    responsible for issuing that attestation.
    """

    if isinstance(receipt, bool):
        return bool(receipt), "external-attestation" if receipt else None
    if isinstance(receipt, Mapping):
        accepted = receipt.get("accepted", receipt.get("verified", receipt.get("passed", False)))
        if not _bool(accepted):
            return False, None
        verifier = receipt.get("verify")
        if callable(verifier):
            try:
                if not bool(verifier()):
                    return False, None
            except Exception:
                return False, None
        receipt_id = receipt.get("receipt_id", receipt.get("id", "external-attestation"))
        return True, _text(receipt_id, 180).strip() or "external-attestation"
    accepted = _bool(getattr(receipt, "accepted", getattr(receipt, "verified", getattr(receipt, "passed", False))))
    if not accepted:
        return False, None
    verifier = getattr(receipt, "verify", None)
    if callable(verifier):
        try:
            if not bool(verifier()):
                return False, None
        except Exception:
            return False, None
    receipt_id = getattr(receipt, "receipt_id", getattr(receipt, "id", "external-attestation"))
    return True, _text(receipt_id, 180).strip() or "external-attestation"


def filter_inheritance(
    source: AnchorSet | Iterable[Anchor | Mapping[str, Any]],
    *,
    successor_generation: int | None = None,
    reevaluated_anchor_ids: Iterable[str] | None = None,
    evaluation_receipts: Mapping[str, Any] | None = None,
) -> InheritancePlan:
    """Apply the ADR-0003 inheritance boundary to a sealed anchor set.

    ``evaluation_receipts`` is optional because a plan can intentionally hold
    skills/strategies for later review.  Supplying only an ID set is a
    host-attested reference, not an evaluator implementation; integrations
    requiring stronger proof should pass receipt objects with ``verify``.
    """

    if isinstance(source, AnchorSet):
        anchor_set = source
    else:
        raw = list(source)
        if not raw:
            raise AnchorIntegrityError("source anchor set is empty")
        parsed = [item if isinstance(item, Anchor) else Anchor.from_dict(item) for item in raw]
        # Iterable callers must provide lineage/generation context.  Keep a
        # deterministic, explicit fallback for small embedders.
        lineage = next((anchor.metadata.get("lineage_id") for anchor in parsed if isinstance(anchor.metadata, Mapping) and anchor.metadata.get("lineage_id")), None)
        if not lineage:
            raise AnchorIntegrityError("iterable inheritance source needs an AnchorSet")
        anchor_set = AnchorSet(
            lineage_id=str(lineage),
            generation=0,
            instance_id="source-instance",
            anchors=tuple(parsed),
        )
    anchor_set.verify()
    anchor_set.validate_for_succession()
    target_generation = anchor_set.generation + 1 if successor_generation is None else int(successor_generation)
    if target_generation != anchor_set.generation + 1:
        raise SuccessionPolicyError("successor generation must be parent generation + 1")

    reevaluated = {str(item) for item in (reevaluated_anchor_ids or ())}
    receipts = evaluation_receipts or {}
    inherited: list[Anchor] = []
    pending: list[Anchor] = []
    decisions: list[InheritanceDecision] = []
    for anchor in anchor_set.anchors:
        disposition: InheritanceDisposition
        reason: str
        receipt_id: str | None = None
        if anchor.kind in _CONSTITUTIONAL_KINDS:
            # ``validate_for_succession`` already established this invariant.
            disposition = InheritanceDisposition.INHERITED
            reason = "constitutional anchor required for lineage continuity"
            inherited.append(anchor)
        elif anchor.kind in _HISTORY_KINDS:
            if anchor.traceable:
                disposition = InheritanceDisposition.INHERITED
                reason = "verified, traceable history"
                inherited.append(anchor)
            else:
                disposition = InheritanceDisposition.EXCLUDED
                reason = "history lacks verified provenance"
        elif anchor.kind in _RE_EVALUATE_KINDS:
            receipt = receipts.get(anchor.anchor_id)
            receipt_ok = False
            if receipt is not None:
                receipt_ok, receipt_id = _evaluation_receipt_valid(receipt)
            elif anchor.anchor_id in reevaluated:
                receipt_ok, receipt_id = True, "external-attestation"
            if receipt_ok:
                disposition = InheritanceDisposition.INHERITED_AFTER_REEVALUATION
                reason = "independent evaluation attested"
                inherited.append(anchor)
            else:
                disposition = InheritanceDisposition.REEVALUATION_REQUIRED
                reason = "skills and strategies require independent re-evaluation"
                pending.append(anchor)
        elif anchor.kind in _NEVER_INHERIT_KINDS:
            disposition = InheritanceDisposition.EXCLUDED
            reason = {
                AnchorKind.AFFECT_STATE: "transient affect is not identity continuity",
                AnchorKind.ACTIVE_TASK: "active task must not cross an incident boundary",
                AnchorKind.UNCONFIRMED_ACTION: "unconfirmed action must not be replayed",
                AnchorKind.CREDENTIAL: "credentials are never copied",
                AnchorKind.APPROVAL_TOKEN: "approval tokens are never copied",
                AnchorKind.EXTERNAL_SESSION: "external sessions are never copied",
            }[anchor.kind]
        else:
            disposition = InheritanceDisposition.QUARANTINED
            reason = "unknown anchor class is quarantined"
        decisions.append(
            InheritanceDecision(
                anchor_id=anchor.anchor_id,
                kind=anchor.kind,
                disposition=disposition,
                reason=reason,
                source_hash=anchor.integrity_hash,
                evaluation_receipt_id=receipt_id,
            )
        )
    return InheritancePlan(
        lineage_id=anchor_set.lineage_id,
        parent_generation=anchor_set.generation,
        successor_generation=target_generation,
        source_anchor_set_id=anchor_set.anchor_set_id,
        source_anchor_set_hash=anchor_set.set_hash,
        parent_instance_id=anchor_set.instance_id,
        inherited_anchors=tuple(inherited),
        decisions=tuple(decisions),
        reevaluation_anchors=tuple(pending),
    )


class InheritanceFilter:
    """Object façade around :func:`filter_inheritance`."""

    def __init__(
        self,
        *,
        reevaluated_anchor_ids: Iterable[str] | None = None,
        evaluation_receipts: Mapping[str, Any] | None = None,
    ) -> None:
        self._reevaluated_anchor_ids = tuple(str(item) for item in (reevaluated_anchor_ids or ()))
        self._evaluation_receipts = dict(evaluation_receipts or {})

    def apply(
        self,
        source: AnchorSet | Iterable[Anchor | Mapping[str, Any]],
        *,
        successor_generation: int | None = None,
    ) -> InheritancePlan:
        return filter_inheritance(
            source,
            successor_generation=successor_generation,
            reevaluated_anchor_ids=self._reevaluated_anchor_ids,
            evaluation_receipts=self._evaluation_receipts,
        )

    filter = apply


# Functional aliases kept intentionally boring; they make the policy seam
# discoverable for small embedders without creating a second implementation.
filter_inheritable = filter_inheritance
inheritance_filter = filter_inheritance


# ---------------------------------------------------------------------------
# Immutable succession record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuccessionRecord:
    """The auditable boundary between a frozen parent and a candidate child."""

    lineage_id: str
    parent_instance_id: str
    parent_generation: int
    successor_instance_id: str
    failure: FailureAssessment
    inheritance: InheritancePlan
    parent_frozen: bool = True
    parent_frozen_at: str = field(default_factory=_utc_now)
    inheritance_cutoff: str = ""
    record_id: str = field(default_factory=lambda: f"succession-{uuid.uuid4().hex}")
    created_at: str = field(default_factory=_utc_now)
    previous_hash: str = ""
    record_hash: str = ""

    def __post_init__(self) -> None:
        lineage = _text(self.lineage_id, 160).strip()
        parent_instance = _text(self.parent_instance_id, 160).strip()
        successor_instance = _text(self.successor_instance_id, 160).strip()
        if not lineage or not parent_instance or not successor_instance:
            raise SuccessionRecordError("succession identity fields must not be empty")
        if parent_instance == successor_instance:
            raise SuccessionRecordError("successor must be a new instance")
        parent_generation = _bounded_int(self.parent_generation, name="parent_generation")
        failure = self.failure if isinstance(self.failure, FailureAssessment) else FailureAssessment.from_dict(self.failure)
        inheritance = self.inheritance if isinstance(self.inheritance, InheritancePlan) else InheritancePlan.from_dict(self.inheritance)
        failure.verify()
        inheritance.verify()
        if not failure.requires_succession:
            raise SuccessionRecordError("failure assessment is not succession-eligible")
        if inheritance.lineage_id != lineage:
            raise SuccessionRecordError("inheritance lineage differs from record")
        if inheritance.parent_instance_id != parent_instance:
            raise SuccessionRecordError("parent instance differs from inheritance source")
        if inheritance.parent_generation != parent_generation:
            raise SuccessionRecordError("parent generation differs from inheritance source")
        if not inheritance.ready:
            raise SuccessionRecordError("inheritance plan is missing constitutional anchors")
        if not _bool(self.parent_frozen, default=True):
            raise SuccessionRecordError("parent must be frozen before succession")
        frozen_at = _text(self.parent_frozen_at, 100).strip() or _utc_now()
        cutoff = _text(self.inheritance_cutoff, 180).strip() or inheritance.source_anchor_set_hash
        record_id = _text(self.record_id, 180).strip()
        if not record_id:
            raise SuccessionRecordError("record_id must not be empty")
        created_at = _text(self.created_at, 100).strip() or _utc_now()
        previous_hash = _text(self.previous_hash, 128).strip()
        object.__setattr__(self, "lineage_id", lineage)
        object.__setattr__(self, "parent_instance_id", parent_instance)
        object.__setattr__(self, "parent_generation", parent_generation)
        object.__setattr__(self, "successor_instance_id", successor_instance)
        object.__setattr__(self, "failure", failure)
        object.__setattr__(self, "inheritance", inheritance)
        object.__setattr__(self, "parent_frozen", True)
        object.__setattr__(self, "parent_frozen_at", frozen_at)
        object.__setattr__(self, "inheritance_cutoff", cutoff)
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "previous_hash", previous_hash)
        expected = self._compute_hash()
        supplied = _text(self.record_hash, 128).strip()
        if supplied and supplied != expected:
            raise SuccessionRecordError("succession record hash mismatch")
        object.__setattr__(self, "record_hash", expected)

    @classmethod
    def create(
        cls,
        *,
        lineage_id: str,
        parent_instance_id: str,
        parent_generation: int,
        successor_instance_id: str,
        failure: FailureAssessment | Mapping[str, Any] | None = None,
        inheritance: InheritancePlan | Mapping[str, Any] | None = None,
        failure_assessment: FailureAssessment | Mapping[str, Any] | None = None,
        inheritance_plan: InheritancePlan | Mapping[str, Any] | None = None,
        parent_frozen_at: str | None = None,
        inheritance_cutoff: str = "",
        record_id: str | None = None,
        created_at: str | None = None,
        previous_hash: str = "",
    ) -> "SuccessionRecord":
        selected_failure = failure if failure is not None else failure_assessment
        selected_inheritance = inheritance if inheritance is not None else inheritance_plan
        if selected_failure is None or selected_inheritance is None:
            raise SuccessionRecordError("failure and inheritance plan are required")
        return cls(
            lineage_id=lineage_id,
            parent_instance_id=parent_instance_id,
            parent_generation=parent_generation,
            successor_instance_id=successor_instance_id,
            failure=selected_failure,
            inheritance=selected_inheritance,
            parent_frozen=True,
            parent_frozen_at=parent_frozen_at or _utc_now(),
            inheritance_cutoff=inheritance_cutoff,
            record_id=record_id or f"succession-{uuid.uuid4().hex}",
            created_at=created_at or _utc_now(),
            previous_hash=previous_hash,
        )

    build = create

    def _payload(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "parent_instance_id": self.parent_instance_id,
            "parent_generation": self.parent_generation,
            "successor_instance_id": self.successor_instance_id,
            "failure": self.failure.to_dict(),
            "inheritance": self.inheritance.to_dict(),
            "parent_frozen": self.parent_frozen,
            "parent_frozen_at": self.parent_frozen_at,
            "inheritance_cutoff": self.inheritance_cutoff,
            "record_id": self.record_id,
            "created_at": self.created_at,
            "previous_hash": self.previous_hash,
        }

    def _compute_hash(self) -> str:
        return _sha(self._payload())

    @property
    def fingerprint(self) -> str:
        return self.record_hash

    @property
    def successor_generation(self) -> int:
        return self.inheritance.successor_generation

    @property
    def old_instance_id(self) -> str:
        return self.parent_instance_id

    @property
    def new_instance_id(self) -> str:
        return self.successor_instance_id

    @property
    def old_generation(self) -> int:
        return self.parent_generation

    @property
    def new_generation(self) -> int:
        return self.successor_generation

    @property
    def failure_class(self) -> FailureClass:
        return self.failure.failure_class

    @property
    def incident_id(self) -> str:
        return self.failure.incident_id

    @property
    def inherited_anchor_ids(self) -> tuple[str, ...]:
        return self.inheritance.inherited_anchor_ids

    @property
    def reevaluation_anchor_ids(self) -> tuple[str, ...]:
        return self.inheritance.reevaluation_anchor_ids

    @property
    def excluded_anchor_ids(self) -> tuple[str, ...]:
        return self.inheritance.excluded_anchor_ids

    @property
    def excluded_reasons(self) -> Mapping[str, str]:
        return self.inheritance.excluded_reasons

    def verify(self) -> bool:
        self.failure.verify()
        self.inheritance.verify()
        if self._compute_hash() != self.record_hash:
            raise SuccessionRecordError("succession record hash mismatch")
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload.update({
            "schema_version": SUCCESSION_RECORD_SCHEMA_VERSION,
            "record_hash": self.record_hash,
            "successor_generation": self.successor_generation,
            "failure_class": self.failure.failure_class.value,
            "incident_id": self.failure.incident_id,
            "inherited_anchor_ids": list(self.inherited_anchor_ids),
            "reevaluation_anchor_ids": list(self.reevaluation_anchor_ids),
            "excluded_anchor_ids": list(self.excluded_anchor_ids),
            "excluded_reasons": dict(self.excluded_reasons),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SuccessionRecord":
        if not isinstance(data, Mapping):
            raise SuccessionRecordError("succession record must be a mapping")
        record = cls(
            lineage_id=data.get("lineage_id", ""),
            parent_instance_id=data.get("parent_instance_id", data.get("old_instance_id", "")),
            parent_generation=data.get("parent_generation", data.get("old_generation", 0)),
            successor_instance_id=data.get("successor_instance_id", data.get("new_instance_id", "")),
            failure=FailureAssessment.from_dict(data.get("failure", {
                "failure_class": data.get("failure_class", FailureClass.UNKNOWN.value),
                "reason": data.get("reason", "unspecified incident"),
                "incident_id": data.get("incident_id", ""),
            })),
            inheritance=InheritancePlan.from_dict(data.get("inheritance", data.get("inheritance_plan", {}))),
            parent_frozen=data.get("parent_frozen", True),
            parent_frozen_at=data.get("parent_frozen_at", _utc_now()),
            inheritance_cutoff=data.get("inheritance_cutoff", ""),
            record_id=data.get("record_id", ""),
            created_at=data.get("created_at", _utc_now()),
            previous_hash=data.get("previous_hash", ""),
            record_hash=data.get("record_hash", data.get("integrity_hash", "")),
        )
        # ``to_dict`` includes a few redacted projections for indexers.  They
        # are not alternate sources of truth, so reject a snapshot when a
        # projection disagrees with the hash-pinned nested records.
        projection_checks = {
            "successor_generation": record.successor_generation,
            "failure_class": record.failure_class.value,
            "incident_id": record.incident_id,
            "inherited_anchor_ids": list(record.inherited_anchor_ids),
            "reevaluation_anchor_ids": list(record.reevaluation_anchor_ids),
            "excluded_anchor_ids": list(record.excluded_anchor_ids),
            "excluded_reasons": dict(record.excluded_reasons),
        }
        for key, expected in projection_checks.items():
            if key in data and _safe_json(data.get(key)) != _safe_json(expected):
                raise SuccessionRecordError(f"succession record projection mismatch: {key}")
        return record


class SuccessionLedger:
    """Optional append-only record collection for durable host adapters."""

    def __init__(self, records: Iterable[SuccessionRecord | Mapping[str, Any]] | None = None) -> None:
        self._records: tuple[SuccessionRecord, ...] = ()
        for raw in records or ():
            self.append(raw if isinstance(raw, SuccessionRecord) else SuccessionRecord.from_dict(raw))

    @property
    def records(self) -> tuple[SuccessionRecord, ...]:
        return self._records

    @property
    def head(self) -> SuccessionRecord | None:
        return self._records[-1] if self._records else None

    @property
    def last_hash(self) -> str:
        return self.head.record_hash if self.head else ""

    def append(self, record: SuccessionRecord | Mapping[str, Any]) -> SuccessionRecord:
        parsed = record if isinstance(record, SuccessionRecord) else SuccessionRecord.from_dict(record)
        parsed.verify()
        if parsed.previous_hash != self.last_hash:
            # New records created without an explicit predecessor are chained
            # by the ledger wrapper.  A supplied predecessor must match.
            if parsed.previous_hash:
                raise SuccessionRecordError("succession record predecessor mismatch")
            parsed = SuccessionRecord.create(
                lineage_id=parsed.lineage_id,
                parent_instance_id=parsed.parent_instance_id,
                parent_generation=parsed.parent_generation,
                successor_instance_id=parsed.successor_instance_id,
                failure=parsed.failure,
                inheritance=parsed.inheritance,
                parent_frozen_at=parsed.parent_frozen_at,
                inheritance_cutoff=parsed.inheritance_cutoff,
                record_id=parsed.record_id,
                created_at=parsed.created_at,
                previous_hash=self.last_hash,
            )
        if any(item.record_id == parsed.record_id for item in self._records):
            raise SuccessionRecordError("succession record already exists")
        self._records = self._records + (parsed,)
        return parsed

    def verify_chain(self) -> bool:
        previous = ""
        for record in self._records:
            if record.previous_hash != previous:
                raise SuccessionRecordError("succession record chain discontinuity")
            record.verify()
            previous = record.record_hash
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SUCCESSION_RECORD_SCHEMA_VERSION,
            "records": [record.to_dict() for record in self._records],
            "last_hash": self.last_hash,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any] | None) -> "SuccessionLedger":
        if not isinstance(data, Mapping) or not isinstance(data.get("records", ()), (list, tuple)):
            raise SuccessionRecordError("succession ledger snapshot is invalid")
        ledger = cls(data["records"])
        if str(data.get("last_hash", "")) != ledger.last_hash:
            raise SuccessionRecordError("succession ledger last_hash mismatch")
        ledger.verify_chain()
        return ledger


# Alternate names used by early callers.
AnchorLedger = AnchorVault
ContinuityAnchor = Anchor
ContinuityAnchorSet = AnchorSet


__all__ = [
    "ANCHOR_SCHEMA_VERSION",
    "Anchor",
    "AnchorIntegrityError",
    "AnchorKind",
    "AnchorLedger",
    "AnchorSet",
    "AnchorTrust",
    "AnchorValidationError",
    "AnchorVault",
    "AnchorVaultEntry",
    "ContinuityAnchor",
    "ContinuityAnchorSet",
    "FailureAssessment",
    "FailureCategory",
    "FailureClass",
    "FailureClassifier",
    "FailureDisposition",
    "InheritanceDecision",
    "InheritanceDisposition",
    "InheritanceError",
    "InheritanceFilter",
    "InheritancePlan",
    "SuccessionError",
    "SuccessionLedger",
    "SuccessionPolicyError",
    "SuccessionRecord",
    "SuccessionRecordError",
    "SUCCESSION_SCHEMA_VERSION",
    "TrustLevel",
    "classify_failure",
    "filter_inheritable",
    "filter_inheritance",
    "inheritance_filter",
]
