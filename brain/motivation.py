"""Bounded motivational pressure and self-iteration proposals.

This module is deliberately a *signal* layer.  It records impulses and
calculates whether a persistent need for change is worth presenting to the
iteration planner.  It never writes source files, executes a proposal, grants
permissions, or treats an impulse as evidence that a change succeeded.

The older ``drive_engine``/``curiosity``/``reward_system`` components remain
useful producers of signals.  ``MotivationalPressure`` is a small, explicit
boundary between those signals and the evaluation pipeline described in
``docs/adr/0002-self-modification-and-evaluation.md``.

Design properties
-----------------
* all values and collections are bounded and JSON serialisable;
* pressure decays exponentially and uses separate trigger/release thresholds
  (hysteresis), so a noisy boundary cannot oscillate;
* rolling frequency and contribution caps prevent a tight loop from creating
  an infinite urge to self-modify;
* self-labelled/internal events have a small weight and a stricter cap.  A
  component cannot vote for itself often enough to manufacture authorization;
* ``IterationNeed`` and ``ChangeProposal`` are records only.  The latter is
  always unevaluated and unauthorized until an independent evaluator handles
  it.

The implementation uses only the Python 3.10+ standard library.
"""

from __future__ import annotations

import math
import hashlib
import hmac
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Any, Callable, Iterable, Mapping


MOTIVATION_SCHEMA_VERSION = 1

MOTIVATION_ATTESTATION_SCHEMA_VERSION = 1
MOTIVATION_ATTESTATION_MAX_TTL = 3600.0
MOTIVATION_ATTESTATION_REPLAY_LIMIT = 4096


@dataclass(frozen=True)
class MotivationSourceAttestation:
    """Host-issued proof that one impulse came from a stable source.

    The signature is deliberately absent from the public snapshot projection;
    a restored event is historical evidence only and must receive a fresh
    host attestation before it can unlock a production iteration need.
    ``nonce`` is retained for compatibility with older callers that did not
    provide one, but newly issued proofs always contain a random nonce.
    """

    attestation_id: str
    issuer_id: str
    source_id: str
    source_kind: str
    event_id: str
    payload_hash: str
    issued_at: float
    expires_at: float
    signature: str = ""
    nonce: str = ""

    def __post_init__(self) -> None:
        for name, limit in (
            ("attestation_id", 160),
            ("issuer_id", 160),
            ("source_id", 160),
            ("source_kind", 40),
            ("event_id", 120),
            ("payload_hash", 128),
            ("signature", 128),
            ("nonce", 160),
        ):
            value = str(getattr(self, name) or "").replace("\x00", "")[:limit].strip()
            if name == "source_kind":
                value = value.lower()
            object.__setattr__(self, name, value)
        try:
            issued = float(self.issued_at)
            expires = float(self.expires_at)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("source attestation timestamps are invalid") from exc
        if not math.isfinite(issued) or not math.isfinite(expires) or expires <= issued:
            raise ValueError("source attestation expiry is invalid")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if not self.attestation_id or not self.issuer_id or not self.source_id:
            raise ValueError("source attestation identifiers are required")
        if not self.event_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", self.event_id):
            raise ValueError("source attestation event id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_hash.lower()):
            raise ValueError("source attestation payload hash is invalid")
        if self.signature and not re.fullmatch(r"[0-9a-f]{64}", self.signature.lower()):
            raise ValueError("source attestation signature is invalid")
        object.__setattr__(self, "payload_hash", self.payload_hash.lower())
        object.__setattr__(self, "signature", self.signature.lower())
        if not self.nonce:
            # Legacy unsigned objects may omit a nonce.  They can never pass
            # ``validate`` because a signed proof is required there.
            object.__setattr__(self, "nonce", "legacy-no-nonce")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": MOTIVATION_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": self.attestation_id,
            "issuer_id": self.issuer_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "event_id": self.event_id,
            "payload_hash": self.payload_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    def signing_payload(self) -> bytes:
        return json.dumps(
            self._payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def attestation_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(include_signature=True), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def replay_key(self) -> str:
        """Opaque durable identity for one issuer/attestation pair."""

        canonical = json.dumps(
            {
                "issuer_id": self.issuer_id,
                "attestation_id": self.attestation_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_signature: bool = False) -> dict[str, Any]:
        if include_signature:
            payload = dict(self._payload())
            payload["signature"] = self.signature
            return payload
        # A public/durable projection must not expose arbitrary host/source
        # labels merely because they do not match a credential-looking regex.
        # The complete signed payload remains process-local for verification;
        # this projection is descriptive and non-authorizing only.
        return {
            "schema_version": MOTIVATION_ATTESTATION_SCHEMA_VERSION,
            "attestation_id": _snapshot_identifier(
                self.attestation_id, "attestation_id", limit=160
            ),
            "issuer_id": _opaque_snapshot_text(self.issuer_id, "issuer_id", limit=160),
            "source_id": _opaque_snapshot_text(self.source_id, "source_id", limit=160),
            "source_kind": _snapshot_category(self.source_kind, "source_kind", limit=40),
            "event_id": _snapshot_identifier(self.event_id, "event_id", limit=120),
            "payload_hash": self.payload_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": _opaque_snapshot_text(self.nonce, "nonce", limit=160),
            # Public projections carry a hash, never the signing material.
            "attestation_hash": self.attestation_hash,
        }

    public_dict = to_dict

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotivationSourceAttestation":
        if not isinstance(data, Mapping):
            raise ValueError("source attestation must be an object")
        if "schema_version" in data and data.get("schema_version") != MOTIVATION_ATTESTATION_SCHEMA_VERSION:
            raise ValueError("unsupported source attestation schema")
        payload = dict(data)
        payload.pop("schema_version", None)
        payload.pop("attestation_hash", None)
        if "signature" not in payload:
            raise ValueError("source attestation signature is required")
        return cls(**payload)

    def verify_signature(self, secret: bytes | bytearray) -> bool:
        if not self.signature or not isinstance(secret, (bytes, bytearray)):
            return False
        expected = hmac.new(bytes(secret), self.signing_payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


class MotivationSourceAttestor:
    """Host-held HMAC issuer and replay guard for motivational sources."""

    def __init__(
        self,
        secret: bytes | str,
        issuer_id: str = "host",
        *,
        clock: Callable[[], float] | None = None,
        max_ttl: float = MOTIVATION_ATTESTATION_MAX_TTL,
        replay_limit: int = MOTIVATION_ATTESTATION_REPLAY_LIMIT,
        replay_store: Any = None,
    ) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(self._secret) < 16:
            raise ValueError("source attestor secret must be at least 16 bytes")
        self.issuer_id = str(issuer_id or "").replace("\x00", "")[:160].strip() or "host"
        self._clock = clock or time.time
        self.max_ttl = float(max_ttl)
        if not math.isfinite(self.max_ttl) or self.max_ttl <= 0:
            raise ValueError("source attestor max_ttl must be positive and finite")
        self._replay_limit = max(16, int(replay_limit))
        self._used_order: deque[str] = deque(maxlen=self._replay_limit)
        self._used: set[str] = set()
        self._lock = RLock()
        if replay_store is not None:
            consume = getattr(replay_store, "consume_motivation_attestation", None)
            lookup = getattr(
                replay_store, "is_motivation_attestation_consumed", None
            )
            if not callable(consume) or not callable(lookup):
                raise TypeError(
                    "replay_store must implement durable motivation replay methods"
                )
        self._replay_store = replay_store

    @staticmethod
    def _coerce_now(value: Any, fallback: Callable[[], float]) -> float:
        if value is None:
            value = fallback()
        try:
            current = float(value)
        except (TypeError, ValueError, OverflowError):
            current = float(fallback())
        if not math.isfinite(current):
            raise ValueError("source attestor clock is not finite")
        return current

    def _remember(self, attestation_id: str) -> None:
        if attestation_id in self._used:
            return
        if len(self._used_order) >= self._used_order.maxlen:
            old = self._used_order.popleft()
            self._used.discard(old)
        self._used_order.append(attestation_id)
        self._used.add(attestation_id)

    def issue(
        self,
        *,
        source_id: str,
        source_kind: str,
        event_id: str,
        payload_hash: str,
        ttl: float = 60.0,
        now: Any = None,
        nonce: str | None = None,
    ) -> MotivationSourceAttestation:
        current = self._coerce_now(now, self._clock)
        ttl_value = float(ttl)
        if not math.isfinite(ttl_value) or ttl_value <= 0 or ttl_value > self.max_ttl:
            raise ValueError("source attestation ttl is outside the host policy")
        unsigned = MotivationSourceAttestation(
            attestation_id=f"mot-att-{uuid.uuid4().hex}",
            issuer_id=self.issuer_id,
            source_id=str(source_id),
            source_kind=str(source_kind),
            event_id=str(event_id),
            payload_hash=str(payload_hash),
            issued_at=current,
            expires_at=current + ttl_value,
            nonce=str(nonce or uuid.uuid4().hex),
            signature="",
        )
        signature = hmac.new(self._secret, unsigned.signing_payload(), hashlib.sha256).hexdigest()
        signed_payload = dict(unsigned._payload())
        signed_payload.pop("schema_version", None)
        signed_payload["signature"] = signature
        return MotivationSourceAttestation(**signed_payload)

    def issue_for_event(
        self,
        event: "ImpulseEvent",
        *,
        source_id: str,
        ttl: float = 60.0,
        now: Any = None,
    ) -> MotivationSourceAttestation:
        return self.issue(
            source_id=source_id,
            source_kind=event.source_kind,
            event_id=event.event_id,
            payload_hash=MotivationalPressure.event_payload_hash(event),
            ttl=ttl,
            now=now,
        )

    def validate(
        self,
        value: MotivationSourceAttestation | Mapping[str, Any],
        *,
        event_id: str,
        payload_hash: str,
        source_id: str | None = None,
        source_kind: str | None = None,
        now: Any = None,
        consume: bool = True,
    ) -> MotivationSourceAttestation:
        attestation = (
            value
            if isinstance(value, MotivationSourceAttestation)
            else MotivationSourceAttestation.from_dict(value)
        )
        current = self._coerce_now(now, self._clock)
        with self._lock:
            if attestation.issuer_id != self.issuer_id:
                raise ValueError("source attestation issuer is not trusted")
            if attestation.attestation_id in self._used:
                raise ValueError("source attestation has already been consumed")
            if current < attestation.issued_at - 5.0 or current > attestation.expires_at + 5.0:
                raise ValueError("source attestation is expired or not yet valid")
            if str(event_id) != attestation.event_id or str(payload_hash).lower() != attestation.payload_hash:
                raise ValueError("source attestation event binding mismatch")
            if source_id is not None and str(source_id) != attestation.source_id:
                raise ValueError("source attestation source binding mismatch")
            if source_kind is not None and str(source_kind).strip().lower() != attestation.source_kind:
                raise ValueError("source attestation kind binding mismatch")
            if not attestation.verify_signature(self._secret):
                raise ValueError("source attestation signature mismatch")
            if consume:
                if self._replay_store is not None:
                    try:
                        consumed = self._replay_store.consume_motivation_attestation(
                            replay_key=attestation.replay_key,
                            attestation_hash=attestation.attestation_hash,
                            consumed_at=current,
                            expires_at=attestation.expires_at,
                        )
                    except Exception as exc:
                        raise ValueError(
                            "durable source attestation replay ledger failed"
                        ) from exc
                    if consumed is not True:
                        raise ValueError(
                            "source attestation has already been consumed"
                        )
                self._remember(attestation.attestation_id)
            elif self._replay_store is not None:
                try:
                    already_consumed = (
                        self._replay_store.is_motivation_attestation_consumed(
                            replay_key=attestation.replay_key
                        )
                    )
                except Exception as exc:
                    raise ValueError(
                        "durable source attestation replay ledger failed"
                    ) from exc
                if already_consumed is not False:
                    raise ValueError(
                        "source attestation has already been consumed"
                    )
        return attestation

    def verify(self, value: MotivationSourceAttestation | Mapping[str, Any], **kwargs: Any) -> bool:
        try:
            self.validate(value, **kwargs, consume=False)
            return True
        except (TypeError, ValueError):
            return False

    @property
    def consumed_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._used)

    @property
    def replay_store(self) -> Any:
        """Return the host replay capability without exposing it in snapshots."""

        return self._replay_store

    @property
    def durable_replay_enabled(self) -> bool:
        """Whether the host supplied an explicitly durable atomic ledger."""

        return bool(
            self._replay_store is not None
            and getattr(
                self._replay_store, "durable_motivation_replay", False
            )
            is True
        )


def _synchronized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize access to one ``MotivationalPressure`` instance.

    ``BrainStem`` normally calls the motivation seam from its event-loop
    thread, but embedders may submit observations, poll needs, or request a
    snapshot from another thread.  The accumulator owns mutable dictionaries,
    deques, and counters, so every public operation must share one lock.  An
    ``RLock`` is used deliberately: public methods compose (for example,
    ``snapshot`` -> ``update`` and ``poll_iteration_need`` ->
    ``should_trigger``) and must not deadlock when they nest.

    The small ``getattr`` fallback keeps the wrapper harmless for a partially
    constructed object should a subclass call a decorated method from its
    initializer; normal instances always install ``_lock`` before callers can
    observe them.
    """

    @wraps(method)
    def wrapped(self, *args: Any, **kwargs: Any):
        lock = getattr(self, "_lock", None)
        if lock is None:
            return method(self, *args, **kwargs)
        with lock:
            return method(self, *args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Small sanitisation helpers
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int, default: str = "") -> str:
    """Return a bounded, printable-ish string.

    We intentionally do not attempt to interpret strings as instructions.
    They are labels/evidence for an independent evaluator, not executable
    content.
    """

    if value is None:
        return default
    try:
        result = str(value)
    except Exception:
        return default
    # NULs make logs/line-oriented stores ambiguous.  Keep the rest of the
    # text because context may legitimately contain non-ASCII characters.
    return result.replace("\x00", "")[: max(0, int(limit))]


def _number(
    value: Any,
    default: float = 0.0,
    low: float | None = None,
    high: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = float(default)
    if not math.isfinite(result):
        result = float(default)
    if low is not None:
        result = max(float(low), result)
    if high is not None:
        result = min(float(high), result)
    return result


def _integer(value: Any, default: int = 0, low: int = 0, high: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = int(default)
    result = max(int(low), result)
    if high is not None:
        result = min(int(high), result)
    return result


def _now_epoch() -> float:
    return time.time()


def _iso_from_epoch(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _epoch(value: Any, default: float | None = None) -> float:
    """Parse epoch seconds, datetime, or an ISO-8601 value.

    ``None`` returns the supplied default (or the current clock).  Invalid
    values are never allowed to create NaN/Infinity in a snapshot.
    """

    if value is None:
        return _now_epoch() if default is None else float(default)
    if isinstance(value, datetime):
        try:
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except (OverflowError, OSError, ValueError):
            return _now_epoch() if default is None else float(default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = _number(value, default if default is not None else _now_epoch())
        return result
    try:
        raw = _text(value, 100).strip()
        # Numeric strings are common in deterministic test/replay feeds.
        # Treat them as epoch seconds before attempting ISO parsing.
        if raw:
            numeric = float(raw)
            if math.isfinite(numeric):
                return numeric
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        result = float(dt.timestamp())
        return result if math.isfinite(result) else (_now_epoch() if default is None else float(default))
    except (TypeError, ValueError, OverflowError, OSError):
        return _now_epoch() if default is None else float(default)


def _valid_timestamp_text(value: Any) -> str:
    """Return a bounded ISO/epoch timestamp, replacing malformed input."""

    if isinstance(value, datetime):
        try:
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt.isoformat()[:100]
        except (OverflowError, OSError, ValueError):
            return _iso_from_epoch(_now_epoch())
    raw = _text(value, 100).strip()
    if raw:
        # A NaN default lets us distinguish a parse failure from a legitimate
        # timestamp without falling back to the current time inside _epoch.
        parsed = _epoch(raw, default=float("nan"))
        if math.isfinite(parsed):
            return raw[:100]
    return _iso_from_epoch(_now_epoch())


def _safe_metadata(value: Any, *, max_items: int = 12, key_limit: int = 60, value_limit: int = 240) -> tuple[tuple[str, Any], ...]:
    """Keep metadata primitive, bounded, and deterministic."""

    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, (list, tuple)):
        # Dataclasses in this module store metadata/budgets as tuples of
        # pairs.  Accept that representation on a round trip, while ignoring
        # malformed entries rather than coercing arbitrary objects.
        pairs = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    else:
        return ()
    result: list[tuple[str, Any]] = []
    for raw_key, raw_value in pairs[:max_items]:
        key = _text(raw_key, key_limit)
        if not key:
            continue
        if isinstance(raw_value, bool):
            clean: Any = raw_value
        elif isinstance(raw_value, int) and not isinstance(raw_value, bool):
            clean = _integer(raw_value, 0, low=-10**9, high=10**9)
        elif isinstance(raw_value, float):
            clean = _number(raw_value, 0.0, low=-1e9, high=1e9)
        elif raw_value is None:
            clean = None
        elif isinstance(raw_value, (str, bytes)):
            clean = _text(raw_value, value_limit)
        else:
            # Do not recursively retain arbitrary objects or nested command
            # payloads.  A short representation is enough for audit context.
            clean = _text(raw_value, value_limit)
        result.append((key, clean))
    return tuple(result)


def _metadata_dict(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    return {str(key): value for key, value in items}


def _bounded_strings(value: Any, *, max_items: int = 16, item_limit: int = 300) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    for item in list(value)[:max_items]:
        text = _text(item, item_limit).strip()
        if text:
            result.append(text)
    return tuple(result)


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Convert a bounded value to JSON-compatible primitives.

    This helper is intentionally conservative and is used at serialization
    boundaries only; it is not a general object encoder.
    """

    if depth > 3:
        return _text(value, 120)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        return {
            _text(k, 80): _json_safe(v, depth=depth + 1)
            for k, v in list(value.items())[:24]
            if _text(k, 80)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:32]]
    return _text(value, 240)


# Motivation observations can originate in user text, tool output, or a
# host adapter.  They are useful as live signals, but raw source/context and
# metadata must not cross the durable snapshot boundary.  Keep this policy
# local to the signal layer so every serializer (including ``ImpulseEvent``
# and ``IterationNeed``) shares the same conservative rules.
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|"
    r"authorization|bearer|cookie|credential|private[_-]?key|session|capability|"
    r"path|root|cwd|working[_-]?directory|source[_-]?path|candidate[_-]?path|url)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:https?://|file://|[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/]|"
    r"/(?:Users|home|root|tmp|var|etc|mnt|opt|srv)(?:[\\/]|$)|"
    r"\b(?:gh[pousr][_-]|sk[_-])[A-Za-z0-9_-]{12,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"(?:api[_-]?key|access[_-]?token|password|secret|authorization|bearer|cookie)\s*[:=])",
    re.IGNORECASE,
)


def _sensitive_text(value: Any, *, key: str = "") -> bool:
    try:
        text = str(value)
    except Exception:
        return True
    return bool(_SENSITIVE_KEY_RE.search(str(key)) or _SENSITIVE_VALUE_RE.search(text))


def _redaction_marker(value: Any, label: str) -> str:
    try:
        raw = str(value)
    except Exception:
        raw = "<unprintable>"
    # Keep a 128-bit digest: it fits the narrowest categorical field (while
    # remaining restart-stable) and avoids the much weaker 64-bit marker that
    # older snapshots used.  The category is normalized through a closed
    # allowlist and can never carry arbitrary caller text.
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"<redacted:{_marker_label(label)}:{digest}>"


_REDACTION_MARKER_RE = re.compile(
    # Accept the legacy 64-bit marker and the current 128-bit marker for
    # restart compatibility; a longer caller-supplied digest is intentionally
    # not trusted and will be re-opaque'd.
    r"^<redacted:([a-z][a-z0-9_.-]{0,39}):((?:[0-9a-f]{16}|[0-9a-f]{32}))>$",
    re.IGNORECASE,
)
# Marker labels are part of the durable projection.  They are categories,
# not a channel for caller-supplied text; keep a closed set so a forged marker
# such as ``<redacted:private-token:...>`` cannot be accepted verbatim.
_SAFE_REDACTION_LABELS = frozenset(
    {
        "source",
        "context",
        "issuer_id",
        "source_id",
        "source_kind",
        "event_id",
        "attestation_id",
        "nonce",
        "impulse_type",
        "motive",
        "trigger",
        "need_id",
        "reason",
        "evidence",
        "source_label",
        "key",
        "value",
        "field",
        "identifier",
        "category",
    }
)
_MAX_SNAPSHOT_MARKER_LENGTH = 128
_IDENTIFIER_SENSITIVE_RE = re.compile(
    r"(?:raw|user|payload|private|secret|token|credential|password|passwd|"
    r"apikey|api[_-]?key|access[_-]?key|authorization|bearer|cookie|"
    r"source[_-]?path|candidate[_-]?path|working[_-]?directory|url)",
    re.IGNORECASE,
)


def _is_redaction_marker(value: Any) -> bool:
    """Whether ``value`` is one of our opaque, restart-stable markers."""

    try:
        match = _REDACTION_MARKER_RE.fullmatch(str(value).strip())
        if not match:
            return False
        label = match.group(1).lower()
        return label in _SAFE_REDACTION_LABELS
    except Exception:
        return False


def _marker_label(value: Any, *, default: str = "value") -> str:
    """Normalize a marker category without retaining arbitrary label text."""

    try:
        label = str(value).strip().lower()
    except Exception:
        label = ""
    return label if label in _SAFE_REDACTION_LABELS else default


def _opaque_snapshot_text(value: Any, label: str, *, limit: int = 500) -> str:
    """Hash a free-form value before it crosses a durable snapshot boundary.

    Older snapshot redaction only handled values that *look* like paths or
    credentials.  Motivational context is user/tool supplied,
    though, so an ordinary sentence is sensitive too.  This helper preserves
    only a deterministic marker (and does not re-hash a marker restored from a
    previous snapshot), allowing bounded metrics and replay bookkeeping while
    keeping the original text out of SQLite/JSON snapshots.
    """

    # Inspect a little beyond the field's normal limit so a valid opaque
    # marker (which contains a 128-bit digest) is not truncated and
    # repeatedly re-hashed on restart.  Non-marker input is still bounded to
    # the requested field limit below.
    raw = _text(value, max(limit, _MAX_SNAPSHOT_MARKER_LENGTH)).strip()
    if _is_redaction_marker(raw):
        return raw
    text = raw[:limit]
    if not text:
        return ""
    return text if _is_redaction_marker(text) else _redaction_marker(text, label)


def _snapshot_identifier(value: Any, label: str, *, limit: int = 120) -> str:
    """Keep machine-shaped IDs readable; hash caller prose or secret-shaped IDs."""

    raw = _text(value, max(limit, _MAX_SNAPSHOT_MARKER_LENGTH)).strip()
    if _is_redaction_marker(raw):
        return raw
    text = raw[:limit]
    if not text:
        return ""
    # Existing integrations use short IDs such as ``c1`` and ``impulse-...``
    # for replay and resolution.  Preserve that narrow grammar while making
    # arbitrary user text, paths, and credential-shaped IDs opaque.
    # Uppercase/ mixed-case IDs are not needed by the built-in replay path and
    # are a common way for opaque payloads (for example ``RAW_USER_DATA`` or
    # cloud key prefixes) to masquerade as identifiers.  Keep the established
    # lowercase machine-ID grammar only.
    if (
        text == text.lower()
        and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,119}", text)
        and not _sensitive_text(text, key=label)
        and not _IDENTIFIER_SENSITIVE_RE.search(text)
    ):
        return text
    return _redaction_marker(text, label)


def _snapshot_category(value: Any, label: str, *, limit: int = 80) -> str:
    """Serialize a bounded categorical label without retaining free-form prose."""

    raw = _text(value, max(limit, _MAX_SNAPSHOT_MARKER_LENGTH)).strip()
    if _is_redaction_marker(raw):
        return raw
    original = raw[:limit]
    text = original.lower()
    if not text:
        return "unknown"
    if (
        original == text
        and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", text)
        and not _sensitive_text(text, key=label)
        and not _IDENTIFIER_SENSITIVE_RE.search(text)
    ):
        return text
    return _redaction_marker(text, label)


def _snapshot_metadata_summary(value: Any) -> dict[str, Any]:
    """Return structure-only metadata plus a digest of the discarded values."""

    normalized = _metadata_dict(_safe_metadata(value))
    # Preserve an already-redacted summary across repeated restart cycles.
    digest = normalized.get("digest")
    if (
        normalized.get("redacted") is True
        and isinstance(normalized.get("item_count"), int)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest.lower())
    ):
        return {
            "redacted": True,
            "item_count": max(0, min(10000, int(normalized["item_count"]))),
            "digest": digest.lower(),
        }
    if not normalized:
        return {}
    canonical = json.dumps(
        _json_safe(normalized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "redacted": True,
        "item_count": len(normalized),
        "digest": hashlib.sha256(canonical).hexdigest(),
    }


def _safe_snapshot_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    max_items: int = 24,
) -> Any:
    """Return bounded JSON data with secret/path-shaped values removed."""

    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:max_items]:
            clean_key = _text(raw_key, 80).strip()
            if not clean_key:
                continue
            if _sensitive_text(clean_key, key=clean_key):
                result[_redaction_marker(clean_key, "key")] = _redaction_marker(raw_value, "value")
            else:
                result[clean_key] = _safe_snapshot_value(
                    raw_value, key=clean_key, depth=depth + 1, max_items=max_items
                )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe_snapshot_value(item, key=key, depth=depth + 1, max_items=max_items)
            for item in list(value)[:max_items]
        ]
    text = _text(value, 500)
    return _redaction_marker(text, _marker_label(key, default="value")) if _sensitive_text(text, key=key) else text


def _integrity_digest(payload: Mapping[str, Any]) -> str:
    """Compute a deterministic, non-secret corruption/tamper marker."""

    encoded = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Impulses
# ---------------------------------------------------------------------------


SOURCE_KINDS = frozenset(
    {
        "external",
        "user",
        "environment",
        "verified",
        "system",
        "internal",
        "self",
        "generated",
        "unknown",
    }
)


def _infer_source_kind(source: str, source_kind: str, self_generated: bool) -> str:
    source_lower = _text(source, 120).lower()
    # A caller cannot bypass the self-vote policy by labelling a source
    # ``external`` while naming it as an internal/self component.
    if self_generated or any(token in source_lower for token in ("self", "internal", "autonomy", "brain", "model")):
        return "self"
    candidate = _text(source_kind, 40).strip().lower()
    if candidate in SOURCE_KINDS:
        return candidate
    if any(token in source_lower for token in ("user", "human", "caller")):
        return "user"
    if any(token in source_lower for token in ("verified", "test", "evaluator")):
        return "verified"
    if any(token in source_lower for token in ("env", "tool", "sensor")):
        return "environment"
    return "unknown"


@dataclass(frozen=True)
class ImpulseEvent:
    """One bounded motivational/emotional observation.

    ``ImpulseEvent`` is an observation, not a command.  ``source_kind`` and
    ``self_generated`` are retained so a pressure aggregator can apply a
    stricter policy to self-reported votes.
    """

    impulse_type: str
    intensity: float
    source: str
    context: str = ""
    timestamp: str = ""
    event_id: str = ""
    persistence: float = 0.0
    unresolved: bool = True
    source_kind: str = ""
    reliability: float = 1.0
    self_generated: bool = False
    metadata: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    provenance: MotivationSourceAttestation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "impulse_type", _text(self.impulse_type, 80).strip().lower() or "unknown")
        object.__setattr__(self, "intensity", _number(self.intensity, 0.0, 0.0, 1.0))
        # Preserve an explicitly empty source so structural-support checks can
        # reject it instead of turning missing provenance into a real label.
        object.__setattr__(self, "source", _text(self.source, 120).strip())
        object.__setattr__(self, "context", _text(self.context, 240).strip())
        # Keep the caller's ISO/epoch timestamp where possible.  Invalid
        # timestamps are replaced rather than leaking malformed values into
        # persistence.
        object.__setattr__(self, "timestamp", _valid_timestamp_text(self.timestamp))
        event_id = _text(self.event_id, 100).strip() or f"impulse-{uuid.uuid4().hex[:16]}"
        if _sensitive_text(event_id, key="event_id"):
            digest = hashlib.sha256(event_id.encode("utf-8", errors="replace")).hexdigest()[:32]
            event_id = f"redacted-event-{digest}"
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "persistence", _number(self.persistence, 0.0, 0.0, 1.0))
        object.__setattr__(self, "unresolved", bool(self.unresolved))
        source_kind = _infer_source_kind(self.source, self.source_kind, bool(self.self_generated))
        object.__setattr__(self, "source_kind", source_kind)
        self_flag = bool(self.self_generated) or source_kind in {"self", "internal", "generated"}
        object.__setattr__(self, "self_generated", self_flag)
        object.__setattr__(self, "reliability", _number(self.reliability, 1.0, 0.0, 1.0))
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))
        if self.provenance is not None and not isinstance(self.provenance, MotivationSourceAttestation):
            try:
                object.__setattr__(
                    self,
                    "provenance",
                    MotivationSourceAttestation.from_dict(self.provenance),
                )
            except Exception:
                # Public snapshots intentionally omit signatures; they restore
                # as historical, non-authorizing events.
                object.__setattr__(self, "provenance", None)

    @property
    def kind(self) -> str:
        """Compatibility alias for callers that call the type ``kind``."""

        return self.impulse_type

    @property
    def type(self) -> str:
        return self.impulse_type

    @property
    def strength(self) -> float:
        return self.intensity

    @property
    def resolved(self) -> bool:
        return not self.unresolved

    @property
    def source_label(self) -> str:
        return self.source

    def to_dict(self) -> dict[str, Any]:
        # ``to_dict`` is the public/persistence representation.  The live
        # frozen object may retain richer context for the current process,
        # but serialized observations never carry free-form source/context or
        # metadata.  This is deliberately stricter than pattern-only secret
        # redaction: an ordinary user sentence is still private data.
        safe_source = _opaque_snapshot_text(self.source, "source", limit=120)
        safe_context = _opaque_snapshot_text(self.context, "context", limit=240)
        safe_event_id = _snapshot_identifier(self.event_id, "event_id", limit=100)
        # Use the same opaque category namespace as the accumulator's state
        # keys.  If a sensitive motive is hashed with a different label here,
        # a restored event would no longer match its restored pressure state.
        safe_impulse_type = _snapshot_category(self.impulse_type, "motive", limit=80)
        return {
            "schema_version": MOTIVATION_SCHEMA_VERSION,
            "event_id": safe_event_id,
            "impulse_type": safe_impulse_type,
            "type": safe_impulse_type,
            "intensity": round(self.intensity, 6),
            "strength": round(self.intensity, 6),
            "source": safe_source,
            "source_label": safe_source,
            "source_kind": self.source_kind,
            "self_generated": self.self_generated,
            "reliability": round(self.reliability, 6),
            "context": safe_context,
            "timestamp": self.timestamp,
            "persistence": round(self.persistence, 6),
            "unresolved": self.unresolved,
            "resolved": not self.unresolved,
            "metadata": _snapshot_metadata_summary(self.metadata),
            # Only a non-authorizing public projection crosses persistence.
            # The signing material remains process-local and is required again
            # for a production event after restart.
            "provenance": self.provenance.public_dict() if self.provenance else None,
        }

    @classmethod
    def create(
        cls,
        impulse_type: str | None = None,
        intensity: float = 0.0,
        source: str = "unknown",
        context: str = "",
        timestamp: Any = None,
        **kwargs: Any,
    ) -> "ImpulseEvent":
        """Construct an event while accepting common legacy aliases."""

        kind = impulse_type
        if kind is None:
            kind = kwargs.pop("kind", kwargs.pop("type", kwargs.pop("motive", "unknown")))
        if "strength" in kwargs:
            intensity = kwargs.pop("strength")
        if "source_label" in kwargs:
            source = kwargs.pop("source_label")
        if timestamp is None:
            timestamp = kwargs.pop("time", kwargs.pop("created_at", ""))
        if "resolved" in kwargs and "unresolved" not in kwargs:
            kwargs["unresolved"] = not bool(kwargs.pop("resolved"))
        return cls(
            impulse_type=kind,
            intensity=intensity,
            source=source,
            context=context,
            timestamp=(
                timestamp.isoformat() if isinstance(timestamp, datetime) else _text(timestamp, 100)
            ),
            event_id=kwargs.pop("event_id", kwargs.pop("id", "")),
            persistence=kwargs.pop("persistence", kwargs.pop("duration_ratio", 0.0)),
            unresolved=kwargs.pop("unresolved", True),
            source_kind=kwargs.pop("source_kind", kwargs.pop("origin", "")),
            reliability=kwargs.pop("reliability", kwargs.pop("confidence", 1.0)),
            self_generated=kwargs.pop("self_generated", kwargs.pop("self_vote", False)),
            metadata=kwargs.pop("metadata", kwargs.pop("meta", {})),
            provenance=kwargs.pop("provenance", kwargs.pop("attestation", None)),
        )

    @classmethod
    def from_dict(cls, data: Any) -> "ImpulseEvent | None":
        if not isinstance(data, Mapping):
            return None
        raw_type = data.get("impulse_type", data.get("type", data.get("kind", data.get("motive", "unknown"))))
        raw_intensity = data.get("intensity", data.get("strength", 0.0))
        unresolved = data.get("unresolved")
        if unresolved is None:
            unresolved = (
                not bool(data.get("resolved"))
                if "resolved" in data
                else True
            )
        return cls.create(
            impulse_type=raw_type,
            intensity=raw_intensity,
            source=data.get("source", data.get("source_label", "unknown")),
            context=data.get("context", ""),
            timestamp=data.get("timestamp", data.get("time", "")),
            event_id=data.get("event_id", data.get("id", "")),
            persistence=data.get("persistence", 0.0),
            unresolved=unresolved,
            source_kind=data.get("source_kind", data.get("origin", "")),
            reliability=data.get("reliability", data.get("confidence", 1.0)),
            self_generated=data.get("self_generated", data.get("self_vote", False)),
            metadata=data.get("metadata", data.get("meta", {})),
            provenance=data.get("provenance", data.get("attestation")),
        )


# ---------------------------------------------------------------------------
# Iteration need and proposal records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationNeed:
    """A bounded request for *evaluation*, never an authorization."""

    motive: str
    pressure: float
    trigger: str = "persistent_pressure"
    reason: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
    source_labels: tuple[str, ...] = field(default_factory=tuple)
    urgency: float = 0.0
    confidence: float = 0.0
    created_at: str = ""
    need_id: str = ""
    requires_evaluation: bool = True
    authorization: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "motive", _text(self.motive, 80).strip().lower() or "unknown")
        object.__setattr__(self, "pressure", _number(self.pressure, 0.0, 0.0, 1.0))
        object.__setattr__(self, "trigger", _text(self.trigger, 60).strip().lower() or "persistent_pressure")
        object.__setattr__(self, "reason", _text(self.reason, 500))
        object.__setattr__(self, "evidence", _bounded_strings(self.evidence, max_items=24, item_limit=300))
        object.__setattr__(self, "source_labels", _bounded_strings(self.source_labels, max_items=16, item_limit=120))
        object.__setattr__(self, "urgency", _number(self.urgency, 0.0, 0.0, 1.0))
        object.__setattr__(self, "confidence", _number(self.confidence, 0.0, 0.0, 1.0))
        stamp = _text(self.created_at, 100).strip()
        if stamp:
            parsed_stamp = _epoch(stamp, default=float("nan"))
            if not math.isfinite(parsed_stamp):
                stamp = _iso_from_epoch(_now_epoch())
        else:
            stamp = _iso_from_epoch(_now_epoch())
        object.__setattr__(self, "created_at", stamp)
        object.__setattr__(self, "need_id", _text(self.need_id, 100).strip() or f"need-{uuid.uuid4().hex[:16]}")
        # These are invariant by design.  A deserialised payload cannot turn a
        # signal into an approval by setting a boolean field.
        object.__setattr__(self, "requires_evaluation", True)
        object.__setattr__(self, "authorization", False)

    @property
    def kind(self) -> str:
        return self.trigger

    @property
    def is_authorized(self) -> bool:
        return False

    @property
    def actionable(self) -> bool:
        """Whether it is worth asking the evaluator to inspect the need."""

        return self.pressure > 0.0 and self.requires_evaluation

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOTIVATION_SCHEMA_VERSION,
            "need_id": _snapshot_identifier(self.need_id, "need_id", limit=100),
            "motive": _snapshot_category(self.motive, "motive", limit=80),
            "pressure": round(self.pressure, 6),
            "trigger": _snapshot_category(self.trigger, "trigger", limit=60),
            "kind": _snapshot_category(self.trigger, "trigger", limit=60),
            "reason": _opaque_snapshot_text(self.reason, "reason", limit=500),
            "evidence": [
                _opaque_snapshot_text(item, "evidence", limit=300)
                for item in self.evidence
            ],
            "source_labels": [
                _opaque_snapshot_text(item, "source_label", limit=120)
                for item in self.source_labels
            ],
            "urgency": round(self.urgency, 6),
            "confidence": round(self.confidence, 6),
            "created_at": self.created_at,
            "requires_evaluation": True,
            "authorization": False,
            "is_authorized": False,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "IterationNeed | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            motive=data.get("motive", data.get("impulse_type", "unknown")),
            pressure=data.get("pressure", 0.0),
            trigger=data.get("trigger", data.get("kind", "persistent_pressure")),
            reason=data.get("reason", ""),
            evidence=data.get("evidence", []),
            source_labels=data.get("source_labels", data.get("sources", [])),
            urgency=data.get("urgency", 0.0),
            confidence=data.get("confidence", 0.0),
            created_at=data.get("created_at", ""),
            need_id=data.get("need_id", data.get("id", "")),
        )


PROTECTED_PROPOSAL_SCOPES = frozenset(
    {
        "life_kernel",
        "lifekernel",
        "life-kernel",
        "identity_core",
        "identitycore",
        "identity_root",
        "evaluation_harness",
        "evaluationharness",
        "evaluator",
        "permissions",
        "credentials",
        "tokens",
        "remote_repository",
        "remote",
        "github",
        "host_environment",
    }
)


@dataclass(frozen=True)
class ChangeProposal:
    """A self-change proposal awaiting independent evaluation.

    This record intentionally has no ``apply``/``write``/``execute`` method.
    ``authorized`` input is ignored and the serialized value is always false.
    """

    title: str
    scope: str
    hypothesis: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    expected_benefits: tuple[str, ...] = field(default_factory=tuple)
    risks: tuple[str, ...] = field(default_factory=tuple)
    resource_budget: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    rollback_revision: str = ""
    baseline_revision: str = ""
    need_id: str = ""
    proposal_id: str = ""
    candidate_revision: str = ""
    created_at: str = ""
    status: str = "proposed"
    requires_external_approval: bool = True
    evaluation_required: bool = True
    authorized: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _text(self.title, 180).strip() or "untitled change")
        object.__setattr__(self, "scope", _text(self.scope, 120).strip().lower() or "unknown")
        object.__setattr__(self, "hypothesis", _text(self.hypothesis, 1000).strip())
        object.__setattr__(self, "evidence", _bounded_strings(self.evidence, max_items=32, item_limit=400))
        object.__setattr__(self, "expected_benefits", _bounded_strings(self.expected_benefits, max_items=16, item_limit=300))
        object.__setattr__(self, "risks", _bounded_strings(self.risks, max_items=24, item_limit=300))
        object.__setattr__(self, "resource_budget", _safe_metadata(self.resource_budget, max_items=16, key_limit=80, value_limit=160))
        object.__setattr__(self, "rollback_revision", _text(self.rollback_revision, 180).strip())
        object.__setattr__(self, "baseline_revision", _text(self.baseline_revision, 180).strip())
        object.__setattr__(self, "need_id", _text(self.need_id, 100).strip())
        object.__setattr__(self, "proposal_id", _text(self.proposal_id, 100).strip() or f"proposal-{uuid.uuid4().hex[:16]}")
        object.__setattr__(self, "candidate_revision", _text(self.candidate_revision, 180).strip())
        stamp = _text(self.created_at, 100).strip() or _iso_from_epoch(_now_epoch())
        object.__setattr__(self, "created_at", stamp)
        normalized_status = _text(self.status, 40).strip().lower() or "proposed"
        if normalized_status in {"approved", "authorized", "applied", "executed", "promoted"}:
            normalized_status = "proposed"
        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(self, "requires_external_approval", True)
        object.__setattr__(self, "evaluation_required", True)
        object.__setattr__(self, "authorized", False)

    @property
    def is_authorized(self) -> bool:
        return False

    @property
    def can_apply(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False

    def validate(self) -> tuple[str, ...]:
        """Return preflight issues; this does not approve or execute anything."""

        errors: list[str] = []
        if not self.hypothesis:
            errors.append("hypothesis_required")
        if not self.evidence:
            errors.append("evidence_required")
        if not self.expected_benefits:
            errors.append("expected_benefits_required")
        if not self.risks:
            errors.append("risks_required")
        if not self.baseline_revision:
            errors.append("baseline_revision_required")
        if not self.rollback_revision:
            errors.append("rollback_revision_required")
        scope_tokens = {
            token
            for token in self.scope.replace("\\", "/").replace(":", "/").split("/")
            if token
        }
        if (
            self.scope in PROTECTED_PROPOSAL_SCOPES
            or bool(scope_tokens & PROTECTED_PROPOSAL_SCOPES)
            or any(token in self.scope for token in ("credential", "token", "github", "life_kernel", "evaluator"))
        ):
            errors.append("protected_scope")
        if not self.resource_budget:
            errors.append("resource_budget_required")
        return tuple(errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOTIVATION_SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "need_id": self.need_id,
            "title": self.title,
            "scope": self.scope,
            "hypothesis": self.hypothesis,
            "evidence": list(self.evidence),
            "expected_benefits": list(self.expected_benefits),
            "risks": list(self.risks),
            "resource_budget": _json_safe(_metadata_dict(self.resource_budget)),
            "rollback_revision": self.rollback_revision,
            "baseline_revision": self.baseline_revision,
            "candidate_revision": self.candidate_revision,
            "created_at": self.created_at,
            "status": self.status,
            "requires_external_approval": True,
            "evaluation_required": True,
            "authorized": False,
            "is_authorized": False,
            "can_apply": False,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ChangeProposal | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            proposal_id=data.get("proposal_id", data.get("id", "")),
            need_id=data.get("need_id", ""),
            title=data.get("title", "untitled change"),
            scope=data.get("scope", data.get("target", "unknown")),
            hypothesis=data.get("hypothesis", ""),
            evidence=data.get("evidence", []),
            expected_benefits=data.get("expected_benefits", data.get("benefits", [])),
            risks=data.get("risks", []),
            resource_budget=data.get("resource_budget", data.get("budget", {})),
            rollback_revision=data.get("rollback_revision", data.get("rollback", "")),
            baseline_revision=data.get("baseline_revision", data.get("baseline", "")),
            candidate_revision=data.get("candidate_revision", data.get("candidate", "")),
            created_at=data.get("created_at", ""),
            status=data.get("status", "proposed"),
        )

    @classmethod
    def from_need(
        cls,
        need: IterationNeed,
        *,
        title: str,
        scope: str,
        hypothesis: str,
        evidence: Iterable[str] = (),
        expected_benefits: Iterable[str] = (),
        risks: Iterable[str] = (),
        resource_budget: Mapping[str, Any] | None = None,
        rollback_revision: str = "",
        baseline_revision: str = "",
    ) -> "ChangeProposal":
        """Create a proposal record from a need; still not authorized."""

        if not isinstance(need, IterationNeed):
            raise TypeError("need must be an IterationNeed")
        evidence_items = list(need.evidence) + list(evidence)
        return cls(
            title=title,
            scope=scope,
            hypothesis=hypothesis,
            evidence=evidence_items,
            expected_benefits=expected_benefits,
            risks=risks,
            resource_budget=resource_budget or {},
            rollback_revision=rollback_revision,
            baseline_revision=baseline_revision,
            need_id=need.need_id,
        )


# ---------------------------------------------------------------------------
# Pressure accumulator
# ---------------------------------------------------------------------------


@dataclass
class _StoredImpulse:
    event: ImpulseEvent
    accepted_at: float
    effective_weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "accepted_at": self.accepted_at,
            "effective_weight": self.effective_weight,
        }


@dataclass
class _MotiveState:
    pressure: float = 0.0
    triggered: bool = False
    trigger_count: int = 0
    last_trigger_at: float = 0.0
    cooldown_until: float = 0.0
    last_event_at: float = 0.0
    event_count: int = 0
    accepted_external: int = 0
    accepted_self: int = 0
    rejected_self: int = 0
    total_intensity: float = 0.0
    total_persistence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pressure": round(self.pressure, 6),
            "triggered": bool(self.triggered),
            "trigger_count": int(self.trigger_count),
            "last_trigger_at": self.last_trigger_at,
            "cooldown_until": self.cooldown_until,
            "last_event_at": self.last_event_at,
            "event_count": int(self.event_count),
            "accepted_external": int(self.accepted_external),
            "accepted_self": int(self.accepted_self),
            "rejected_self": int(self.rejected_self),
            "total_intensity": round(self.total_intensity, 6),
            "total_persistence": round(self.total_persistence, 6),
        }


class MotivationalPressure:
    """Aggregate bounded impulse events into decaying motivational pressure.

    The class supports multiple motive types (``curiosity``, ``growth``,
    ``coherence`` ...).  A motive can produce an ``IterationNeed`` through
    :meth:`poll_iteration_need`; that method only emits a record for an
    independent evaluator and never grants code-writing authority.
    """

    # Public defaults make policy inspectable and easy to seal in an evaluator.
    DEFAULT_THRESHOLD = 0.72
    DEFAULT_RELEASE_THRESHOLD = 0.45
    DEFAULT_DECAY_RATE = 0.01  # exponential fraction per second
    DEFAULT_FREQUENCY_WINDOW = 300.0
    DEFAULT_FREQUENCY_TARGET = 4
    DEFAULT_CONTEXT_TARGET = 3
    DEFAULT_UNRESOLVED_TARGET = 3
    DEFAULT_COOLDOWN = 120.0
    DEFAULT_RATE_CAP = 12
    DEFAULT_SELF_RATE_CAP = 3
    DEFAULT_SELF_WEIGHT = 0.20
    DEFAULT_SELF_CONTRIBUTION_CAP = 0.28
    DEFAULT_EVENT_GAIN = 0.35
    DEFAULT_EVENT_GAIN_CAP = 0.22
    DEFAULT_MAX_EVENTS = 256

    SOURCE_WEIGHTS = {
        "external": 1.0,
        "user": 1.0,
        "verified": 1.0,
        "environment": 0.9,
        "system": 0.8,
        "unknown": 0.35,
        "internal": DEFAULT_SELF_WEIGHT,
        "self": DEFAULT_SELF_WEIGHT,
        "generated": DEFAULT_SELF_WEIGHT,
    }

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        release_threshold: float = DEFAULT_RELEASE_THRESHOLD,
        decay_rate: float = DEFAULT_DECAY_RATE,
        frequency_window: float = DEFAULT_FREQUENCY_WINDOW,
        frequency_target: int = DEFAULT_FREQUENCY_TARGET,
        context_target: int = DEFAULT_CONTEXT_TARGET,
        unresolved_target: int = DEFAULT_UNRESOLVED_TARGET,
        cooldown_seconds: float = DEFAULT_COOLDOWN,
        rate_cap: int = DEFAULT_RATE_CAP,
        max_events: int = DEFAULT_MAX_EVENTS,
        self_rate_cap: int = DEFAULT_SELF_RATE_CAP,
        self_weight: float = DEFAULT_SELF_WEIGHT,
        self_contribution_cap: float = DEFAULT_SELF_CONTRIBUTION_CAP,
        event_gain: float = DEFAULT_EVENT_GAIN,
        event_gain_cap: float = DEFAULT_EVENT_GAIN_CAP,
        clock: Callable[[], float] | None = None,
        source_attestor: MotivationSourceAttestor | None = None,
        require_source_attestation: bool = False,
        source_verifier: Callable[..., bool] | None = None,
        **aliases: Any,
    ) -> None:
        # All mutable pressure state below is guarded by this per-instance
        # re-entrant lock.  A single lock keeps compound operations such as
        # decay -> prune -> record and snapshot -> update atomic to callers
        # from the BrainStem thread or an embedding worker thread.
        self._lock = RLock()
        required = bool(require_source_attestation)
        if source_attestor is not None and not isinstance(
            source_attestor, MotivationSourceAttestor
        ):
            raise TypeError(
                "source_attestor must be a MotivationSourceAttestor"
            )
        if required and source_verifier is not None:
            raise TypeError(
                "strict source attestation does not accept an arbitrary source_verifier"
            )
        # These are policy capabilities, not live tuning knobs.  Keep the
        # backing values private and expose read-only properties below so an
        # embedding caller cannot downgrade a production accumulator after
        # construction.
        self._source_attestor = source_attestor
        self._require_source_attestation = required
        self._source_verifier = source_verifier
        # Accept names used by a few embedders without making policy mutable
        # through arbitrary kwargs.
        if "trigger_threshold" in aliases:
            threshold = aliases["trigger_threshold"]
        if "reset_threshold" in aliases:
            release_threshold = aliases["reset_threshold"]
        if "decay" in aliases:
            decay_rate = aliases["decay"]
        if "window_seconds" in aliases:
            frequency_window = aliases["window_seconds"]
        if "rate_limit" in aliases:
            rate_cap = aliases["rate_limit"]
        if "self_vote_weight" in aliases:
            self_weight = aliases["self_vote_weight"]

        self.threshold = _number(threshold, self.DEFAULT_THRESHOLD, 0.01, 1.0)
        self.release_threshold = _number(release_threshold, self.DEFAULT_RELEASE_THRESHOLD, 0.0, self.threshold)
        self.decay_rate = _number(decay_rate, self.DEFAULT_DECAY_RATE, 0.0, 10.0)
        self.frequency_window = _number(frequency_window, self.DEFAULT_FREQUENCY_WINDOW, 1.0, 86400.0)
        self.frequency_target = _integer(frequency_target, self.DEFAULT_FREQUENCY_TARGET, 1, 1000)
        self.context_target = _integer(context_target, self.DEFAULT_CONTEXT_TARGET, 1, 100)
        self.unresolved_target = _integer(unresolved_target, self.DEFAULT_UNRESOLVED_TARGET, 1, 100)
        self.cooldown_seconds = _number(cooldown_seconds, self.DEFAULT_COOLDOWN, 0.0, 86400.0)
        self.rate_cap = _integer(rate_cap, self.DEFAULT_RATE_CAP, 1, 10000)
        self.max_events = _integer(max_events, self.DEFAULT_MAX_EVENTS, 1, 10000)
        self.self_rate_cap = _integer(self_rate_cap, self.DEFAULT_SELF_RATE_CAP, 0, self.rate_cap)
        self.self_weight = _number(self_weight, self.DEFAULT_SELF_WEIGHT, 0.0, 1.0)
        self.self_contribution_cap = _number(self_contribution_cap, self.DEFAULT_SELF_CONTRIBUTION_CAP, 0.0, 1.0)
        self.event_gain = _number(event_gain, self.DEFAULT_EVENT_GAIN, 0.0, 10.0)
        self.event_gain_cap = _number(event_gain_cap, self.DEFAULT_EVENT_GAIN_CAP, 0.0, 1.0)
        self._clock = clock or _now_epoch

        self._states: dict[str, _MotiveState] = {}
        self._events: deque[_StoredImpulse] = deque(maxlen=self.max_events)
        self._seen_ids: deque[str] = deque(maxlen=max(self.max_events * 2, 16))
        self._seen_id_set: set[str] = set()
        self._resolved_ids: set[str] = set()
        # A consumed attestation is remembered by event id for the lifetime of
        # this accumulator.  It lets structural-support metrics use the
        # attested source identity without asking the host attestor to consume
        # (and therefore reject) the same one-shot proof a second time.
        self._verified_provenance: dict[str, MotivationSourceAttestation] = {}
        self._last_update = _number(self._clock(), _now_epoch())
        self.total_received = 0
        self.total_accepted = 0
        self.total_rejected = 0
        self.total_self_rejected = 0
        self.total_needs_emitted = 0
        # Set by from_snapshot when a persisted payload fails its integrity
        # marker.  A rejected restore is fail-closed (empty pressure), never
        # an authorization path.
        self.snapshot_rejected = False
        self.restore_error = ""

    @property
    def source_attestor(self) -> MotivationSourceAttestor | None:
        return self._source_attestor

    @property
    def require_source_attestation(self) -> bool:
        return self._require_source_attestation

    @property
    def source_verifier(self) -> Callable[..., bool] | None:
        return self._source_verifier

    # -- basic state -----------------------------------------------------

    @property
    @_synchronized
    def events(self) -> tuple[ImpulseEvent, ...]:
        return tuple(item.event for item in self._events)

    @property
    @_synchronized
    def motives(self) -> tuple[str, ...]:
        return tuple(sorted(self._states))

    def _state(self, motive: str) -> _MotiveState:
        key = _text(motive, 80).strip().lower() or "unknown"
        return self._states.setdefault(key, _MotiveState())

    def _time(self, value: Any = None) -> float:
        if value is None:
            try:
                result = float(self._clock())
            except Exception:
                result = _now_epoch()
        else:
            result = _epoch(value, default=self._last_update)
        return result if math.isfinite(result) else self._last_update

    @_synchronized
    def _decay(self, now: float) -> None:
        # Clock rollback is treated as zero elapsed time; it must not increase
        # pressure or reopen a cooldown.
        # A brand-new accumulator may receive a deterministic replay timestamp
        # (for example ``now=0``) while its default wall clock was initialised
        # to the present.  With no state/events yet it is safe to adopt that
        # replay epoch; once evidence exists, clock rollback remains clamped.
        if (
            now < self._last_update
            and not self._events
            and not any(state.pressure or state.cooldown_until for state in self._states.values())
        ):
            self._last_update = now
        elapsed = max(0.0, now - self._last_update)
        if elapsed and self.decay_rate:
            factor = math.exp(-self.decay_rate * elapsed)
            for state in self._states.values():
                state.pressure = max(0.0, min(1.0, state.pressure * factor))
        self._last_update = max(self._last_update, now)
        # Trigger state is hysteretic: it only clears below the lower bound.
        for state in self._states.values():
            if state.triggered and state.pressure <= self.release_threshold:
                state.triggered = False
        self._prune_events(now)

    @_synchronized
    def update(self, now: Any = None) -> dict[str, float]:
        """Apply time decay and return current pressures by motive."""

        stamp = self._time(now)
        self._decay(stamp)
        return {motive: round(state.pressure, 6) for motive, state in self._states.items()}

    tick = update

    def _prune_events(self, now: float) -> None:
        cutoff = now - self.frequency_window
        while self._events and self._events[0].accepted_at < cutoff:
            self._events.popleft()
        # ``_seen_ids`` is a replay guard, not an unbounded audit log.  Keep
        # recent IDs; IDs of pruned events may be accepted again only after the
        # bounded guard has rotated, which is an explicit resource trade-off.
        live_ids = {item.event.event_id for item in self._events}
        while self._seen_ids and len(self._seen_id_set) > self._seen_ids.maxlen:
            old = self._seen_ids.popleft()
            self._seen_id_set.discard(old)
        # Do not remove live IDs from the set; they remain deduplicated.
        self._resolved_ids.intersection_update({item.event.event_id for item in self._events} | live_ids)

    def _source_weight(self, event: ImpulseEvent) -> float:
        kind = event.source_kind
        if event.self_generated or kind in {"self", "internal", "generated"}:
            return self.self_weight
        return _number(self.SOURCE_WEIGHTS.get(kind, self.SOURCE_WEIGHTS["unknown"]), 0.35, 0.0, 1.0)

    @staticmethod
    def event_payload_hash(event: ImpulseEvent) -> str:
        """Hash the complete normalized, provenance-free live event.

        Hosts issue a source attestation against this exact canonical payload;
        changing even one field (including source/context, source kind or
        reliability) makes the proof unusable.  This payload is hashed only in
        memory and is never persisted; the public serializer remains strictly
        redacted, so paths/tokens cannot cross the snapshot boundary.
        """

        # Do not hash the public redacted projection here.  Doing so would
        # make a visible marker an interchangeable substitute for the hidden
        # source/context that the host actually attested.  Keep the canonical
        # fields bounded by ``ImpulseEvent``'s constructor, but retain their
        # live values for the HMAC binding only.
        payload = {
            "schema_version": MOTIVATION_SCHEMA_VERSION,
            "event_id": event.event_id,
            "impulse_type": event.impulse_type,
            "intensity": round(event.intensity, 6),
            "source": event.source,
            "source_kind": event.source_kind,
            "self_generated": bool(event.self_generated),
            "reliability": round(event.reliability, 6),
            "context": event.context,
            "timestamp": event.timestamp,
            "persistence": round(event.persistence, 6),
            "unresolved": bool(event.unresolved),
            "metadata": _json_safe(_metadata_dict(event.metadata)),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _canonical_source(event: ImpulseEvent) -> str:
        """Return the normalized caller label for explicit legacy mode.

        This branch is retained solely for old embedders.  A production
        ``BrainStem`` enables ``require_source_attestation`` and never calls
        it for structural support; labels such as ``verified`` are not proof.
        """

        return event.source.strip().casefold()

    def _attested_source(self, event: ImpulseEvent) -> str:
        proof = self._verified_provenance.get(event.event_id)
        if proof is None:
            return ""
        if proof.event_id != event.event_id:
            return ""
        if proof.payload_hash != self.event_payload_hash(event):
            return ""
        if proof.source_kind != event.source_kind:
            return ""
        return proof.source_id.strip().casefold()

    def _is_independent_support(self, item: _StoredImpulse) -> bool:
        """Whether one retained event can provide structural support.

        Zero-trust or already-resolved observations may remain useful for
        frequency/history, but they cannot unlock an iteration need.  The
        effective weight incorporates source policy and reliability.
        """

        event = item.event
        if self.require_source_attestation:
            source_identity = self._attested_source(event)
        else:
            source_identity = self._attested_source(event) or self._canonical_source(event)
        return bool(
            not event.self_generated
            and event.source_kind
            in {"external", "user", "environment", "verified", "system"}
            and event.intensity > 0.0
            and item.effective_weight > 0.0
            and event.unresolved
            and event.event_id not in self._resolved_ids
            and source_identity
        )

    def _recent_for(self, motive: str, now: float) -> list[_StoredImpulse]:
        cutoff = now - self.frequency_window
        return [item for item in self._events if item.event.impulse_type == motive and item.accepted_at >= cutoff]

    def _self_contribution(self, motive: str, now: float) -> float:
        return sum(
            item.event.intensity * item.effective_weight
            for item in self._recent_for(motive, now)
            if item.event.self_generated
        )

    def _components_for(self, motive: str, now: float) -> dict[str, float | int]:
        """Return the bounded observables used by the pressure calculation."""

        motive = _text(motive, 80).strip().lower() or "unknown"
        recent = self._recent_for(motive, now)
        contexts = {
            item.event.context.strip().lower()
            for item in recent
            if item.event.context.strip()
        }
        intensity = sum(
            item.event.intensity * item.effective_weight for item in recent
        ) / max(1, len(recent))
        total_weight = sum(item.effective_weight for item in recent)
        explicit_persistence = sum(
            item.event.persistence * item.effective_weight for item in recent
        ) / max(1e-9, total_weight)
        span = (
            max(item.accepted_at for item in recent)
            - min(item.accepted_at for item in recent)
            if recent
            else 0.0
        )
        temporal_persistence = min(1.0, max(0.0, span / self.frequency_window))
        persistence = max(explicit_persistence, temporal_persistence)
        frequency = min(1.0, len(recent) / float(self.frequency_target))
        cross_context = min(1.0, len(contexts) / float(self.context_target))
        unresolved_count = sum(
            1
            for item in recent
            if item.event.unresolved and item.event.event_id not in self._resolved_ids
        )
        unresolved = min(1.0, unresolved_count / float(self.unresolved_target))
        independent_sources = {
            (
                self._attested_source(item.event)
                if self.require_source_attestation
                else self._attested_source(item.event) or self._canonical_source(item.event)
            )
            for item in recent
            if self._is_independent_support(item)
        }
        return {
            "frequency": round(frequency, 6),
            "event_count": len(recent),
            "intensity": round(min(1.0, max(0.0, intensity)), 6),
            "persistence": round(min(1.0, max(0.0, persistence)), 6),
            "unresolved": round(unresolved, 6),
            "unresolved_count": unresolved_count,
            "cross_context": round(cross_context, 6),
            "context_count": len(contexts),
            "independent_source_count": len(independent_sources),
        }

    def _remember_id(self, event_id: str) -> None:
        if event_id in self._seen_id_set:
            return
        if self._seen_ids.maxlen and len(self._seen_ids) >= self._seen_ids.maxlen:
            evicted = self._seen_ids.popleft()
            self._seen_id_set.discard(evicted)
        self._seen_ids.append(event_id)
        self._seen_id_set.add(event_id)

    # -- recording -------------------------------------------------------

    @_synchronized
    def record(
        self,
        event: ImpulseEvent | Mapping[str, Any],
        now: Any = None,
        *,
        provenance: MotivationSourceAttestation | Mapping[str, Any] | None = None,
    ) -> float:
        """Record one impulse and return its motive's current pressure.

        ``False``-like malformed/duplicate/rate-capped observations are
        ignored.  The return value remains the current pressure, allowing a
        caller to inspect the result without treating acceptance as approval.
        Use :meth:`accept` when an explicit boolean is preferred.
        """

        self.total_received = min(10**12, self.total_received + 1)
        impulse = event if isinstance(event, ImpulseEvent) else ImpulseEvent.from_dict(event)
        if impulse is None:
            self.total_rejected = min(10**12, self.total_rejected + 1)
            return 0.0
        if provenance is not None and impulse.provenance is None:
            try:
                impulse = replace(impulse, provenance=provenance)
            except Exception:
                self.total_rejected = min(10**12, self.total_rejected + 1)
                return 0.0
        stamp = self._time(now)
        self._decay(stamp)
        motive = impulse.impulse_type
        state = self._state(motive)
        if impulse.event_id in self._seen_id_set:
            self.total_rejected = min(10**12, self.total_rejected + 1)
            return state.pressure

        recent = self._recent_for(motive, stamp)
        is_self = impulse.self_generated or impulse.source_kind in {"self", "internal", "generated"}
        if len(recent) >= self.rate_cap:
            self.total_rejected = min(10**12, self.total_rejected + 1)
            if is_self:
                self.total_self_rejected = min(10**12, self.total_self_rejected + 1)
                state.rejected_self = min(10**9, state.rejected_self + 1)
            return state.pressure
        if is_self:
            self_count = sum(1 for item in recent if item.event.self_generated)
            if self_count >= self.self_rate_cap:
                self.total_rejected = min(10**12, self.total_rejected + 1)
                self.total_self_rejected = min(10**12, self.total_self_rejected + 1)
                state.rejected_self = min(10**9, state.rejected_self + 1)
                return state.pressure

        weight = self._source_weight(impulse) * impulse.reliability
        if is_self:
            # A self-report may contribute, but the rolling contribution cap
            # prevents self-voting from crossing the entire threshold alone.
            remaining = self.self_contribution_cap - self._self_contribution(motive, stamp)
            if remaining <= 0.0:
                self.total_rejected = min(10**12, self.total_rejected + 1)
                self.total_self_rejected = min(10**12, self.total_self_rejected + 1)
                state.rejected_self = min(10**9, state.rejected_self + 1)
                return state.pressure
            weight = min(weight, remaining / max(impulse.intensity, 1e-9))
        weight = _number(weight, 0.0, 0.0, 1.0)

        # Perform all cheap admission checks before consuming a one-shot host
        # proof.  A duplicate, rate-capped, or self-cap event must not let an
        # untrusted caller burn a valid attestation (a denial-of-service
        # vector).  The proof is consumed only immediately before the bounded
        # append below.
        verified_proof: MotivationSourceAttestation | None = None
        if self.require_source_attestation or impulse.provenance is not None:
            if impulse.provenance is None:
                if self.require_source_attestation:
                    self.total_rejected = min(10**12, self.total_rejected + 1)
                    return state.pressure
            elif self.source_attestor is None and self.source_verifier is None:
                if self.require_source_attestation:
                    self.total_rejected = min(10**12, self.total_rejected + 1)
                    return state.pressure
            else:
                try:
                    if self.source_attestor is not None:
                        verified_proof = self.source_attestor.validate(
                            impulse.provenance,
                            event_id=impulse.event_id,
                            payload_hash=self.event_payload_hash(impulse),
                            source_kind=impulse.source_kind,
                            now=stamp,
                            consume=True,
                        )
                    else:
                        verifier = self.source_verifier
                        assert verifier is not None
                        try:
                            verified = bool(verifier(impulse, impulse.provenance))
                        except TypeError:
                            verified = bool(verifier(impulse))
                        if not verified:
                            raise ValueError("source verifier rejected event")
                        verified_proof = impulse.provenance
                except Exception:
                    if self.require_source_attestation:
                        self.total_rejected = min(10**12, self.total_rejected + 1)
                        return state.pressure
                    # An optional/legacy proof that cannot be verified is
                    # treated as an ordinary legacy observation; it never
                    # becomes an independent principal.
                    verified_proof = None

        stored = _StoredImpulse(impulse, stamp, weight)
        self._events.append(stored)
        if verified_proof is not None:
            self._verified_provenance[impulse.event_id] = verified_proof
            while len(self._verified_provenance) > self.max_events:
                oldest_id = next(iter(self._verified_provenance))
                self._verified_provenance.pop(oldest_id, None)
        self._remember_id(impulse.event_id)
        self.total_accepted = min(10**12, self.total_accepted + 1)
        if is_self:
            state.accepted_self = min(10**9, state.accepted_self + 1)
        else:
            state.accepted_external = min(10**9, state.accepted_external + 1)
        state.event_count = min(10**9, state.event_count + 1)
        state.last_event_at = stamp
        state.total_intensity = min(10**12, state.total_intensity + impulse.intensity)
        state.total_persistence = min(10**12, state.total_persistence + impulse.persistence)

        components = self._components_for(motive, stamp)
        intensity = float(components["intensity"])
        persistence = float(components["persistence"])
        frequency = float(components["frequency"])
        cross_context = float(components["cross_context"])
        unresolved = float(components["unresolved"])
        signal = (
            0.40 * intensity
            + 0.20 * frequency
            + 0.16 * persistence
            + 0.14 * cross_context
            + 0.10 * unresolved
        )
        # Apply the newest event's trust weight again only as a bounded gain;
        # the rolling terms already include weights from previous events.
        delta = min(self.event_gain_cap, max(0.0, signal * self.event_gain * max(weight, 0.05)))
        state.pressure = max(0.0, min(1.0, state.pressure + delta))
        return state.pressure

    @_synchronized
    def accept(
        self,
        event: ImpulseEvent | Mapping[str, Any],
        now: Any = None,
        *,
        provenance: MotivationSourceAttestation | Mapping[str, Any] | None = None,
    ) -> bool:
        """Record an event and report whether it was accepted."""

        before = self.total_accepted
        self.record(event, now=now, provenance=provenance)
        return self.total_accepted > before

    add_event = record
    observe = record

    def record_impulse(
        self,
        impulse_type: str,
        intensity: float,
        source: str,
        context: str = "",
        timestamp: Any = None,
        now: Any = None,
        **kwargs: Any,
    ) -> float:
        """Create and record one impulse using the caller's replay clock.

        ``timestamp`` is the event's descriptive time, while ``now`` is the
        accumulator clock used for decay/window admission.  Keeping the two
        explicit makes deterministic replay possible and avoids silently
        consulting wall clock time when a host supplied a fixed tick.
        """
        event = ImpulseEvent.create(
            impulse_type=impulse_type,
            intensity=intensity,
            source=source,
            context=context,
            timestamp=timestamp,
            **kwargs,
        )
        return self.record(event, now=now)

    add_impulse = record_impulse

    # -- resolution and trigger ----------------------------------------

    @_synchronized
    def resolve(self, event_id: str | None = None, motive: str | None = None, now: Any = None) -> int:
        """Mark recent unresolved impulses as addressed.

        Resolution is evidence that a pressure source was handled; it is not
        evidence that a code change succeeded.  The relief is deliberately
        gradual and bounded by normal decay.
        """

        stamp = self._time(now)
        self._decay(stamp)
        target_ids: set[str] = set()
        for item in self._recent_for(_text(motive, 80).lower(), stamp) if motive else self._events:
            if not item.event.unresolved or item.event.event_id in self._resolved_ids:
                continue
            if event_id and item.event.event_id != _text(event_id, 100):
                continue
            target_ids.add(item.event.event_id)
            if event_id:
                break
        if not target_ids:
            return 0
        self._resolved_ids.update(target_ids)
        # A small relief avoids a sticky score while preserving hysteresis.
        for motive_key, state in self._states.items():
            relevant = len([i for i in target_ids if any(j.event.event_id == i and j.event.impulse_type == motive_key for j in self._events)])
            if relevant:
                state.pressure = max(0.0, state.pressure - min(0.15, relevant * 0.04))
        self._decay(stamp)
        return len(target_ids)

    mark_resolved = resolve

    @_synchronized
    def pressure_for(self, motive: str) -> float:
        return round(self._state(motive).pressure, 6)

    @property
    def current_pressure(self) -> dict[str, float]:
        """Read-only pressure projection for all known motives."""

        return self.get_pressure()  # type: ignore[return-value]

    @_synchronized
    def get_metrics(self, motive: str, now: Any = None) -> dict[str, float | int]:
        """Expose frequency/intensity/persistence components for audit/UI."""

        stamp = self._time(now)
        self._decay(stamp)
        result = dict(self._components_for(motive, stamp))
        result["pressure"] = round(self._state(motive).pressure, 6)
        return result

    metrics = get_metrics

    @_synchronized
    def get_pressure(self, motive: str | None = None) -> float | dict[str, float]:
        if motive is None:
            self.update()
            return {key: round(value.pressure, 6) for key, value in self._states.items()}
        self.update()
        return self.pressure_for(motive)

    @_synchronized
    def is_triggered(self, motive: str) -> bool:
        self.update()
        return bool(self._state(motive).triggered)

    @_synchronized
    def trigger_ready(self, motive: str, now: Any = None) -> bool:
        stamp = self._time(now)
        self._decay(stamp)
        state = self._state(motive)
        independent_support = int(
            self._components_for(motive, stamp)["independent_source_count"]
        )
        return (
            state.pressure >= self.threshold
            and not state.triggered
            and stamp >= state.cooldown_until
            and independent_support > 0
        )

    @_synchronized
    def should_trigger(self, motive: str, now: Any = None, consume: bool = False) -> bool:
        ready = self.trigger_ready(motive, now=now)
        if ready and consume:
            state = self._state(motive)
            stamp = self._time(now)
            state.triggered = True
            state.trigger_count = min(10**9, state.trigger_count + 1)
            state.last_trigger_at = stamp
            state.cooldown_until = stamp + self.cooldown_seconds
        return ready

    def _need_for(self, motive: str, *, trigger: str, reason: str, now: float) -> IterationNeed:
        state = self._state(motive)
        recent = self._recent_for(motive, now)
        tail = recent[-8:]
        support = [item for item in recent if self._is_independent_support(item)]
        # Keep the bounded recent tail for observability, but append any
        # structural-support events that fell just outside it so the emitted
        # need explains the same evidence used by ``trigger_ready``.
        evidence_items = list(tail)
        for item in support:
            if item not in evidence_items:
                evidence_items.append(item)
        evidence_items = evidence_items[-16:]
        evidence = tuple(
            f"impulse:{item.event.event_id}:{item.event.source}"
            for item in evidence_items
        )
        sources = tuple(
            dict.fromkeys(
                item.event.source.strip()
                for item in evidence_items
                if item.event.source.strip()
            )
        )
        urgency = min(1.0, state.pressure * (1.2 if trigger in {"incident", "safety"} else 1.0))
        confidence = min(
            1.0,
            0.45 * min(1.0, len(recent) / max(1, self.frequency_target))
            + 0.30 * min(1.0, len({item.event.context for item in recent if item.event.context}) / max(1, self.context_target))
            + 0.25 * state.pressure,
        )
        return IterationNeed(
            motive=motive,
            pressure=state.pressure,
            trigger=trigger,
            reason=reason or "persistent motivational pressure",
            evidence=evidence,
            source_labels=sources,
            urgency=urgency,
            confidence=confidence,
            created_at=_iso_from_epoch(now),
        )

    @_synchronized
    def poll_iteration_need(
        self,
        motive: str | None = None,
        now: Any = None,
        *,
        reason: str = "",
        trigger: str = "persistent_pressure",
    ) -> IterationNeed | None:
        """Consume one threshold crossing and return an unevaluated need."""

        stamp = self._time(now)
        self._decay(stamp)
        candidates = [motive] if motive else list(self._states)
        # Stable ordering avoids nondeterministic choices when several motives
        # cross simultaneously.
        for candidate in sorted(_text(item, 80).lower() for item in candidates if item):
            if not self.should_trigger(candidate, now=stamp, consume=True):
                continue
            state = self._state(candidate)
            need = self._need_for(candidate, trigger=trigger, reason=reason, now=stamp)
            # Defensive invariant: a need can never carry authorization even
            # if a future subclass attempts to mutate its payload.
            if need.is_authorized:
                continue
            self.total_needs_emitted = min(10**12, self.total_needs_emitted + 1)
            return need
        return None

    emit_iteration_need = poll_iteration_need
    evaluate_need = poll_iteration_need

    @_synchronized
    def record_incident(
        self,
        motive: str,
        reason: str,
        *,
        severity: float = 1.0,
        source: str = "system",
        context: str = "incident",
        now: Any = None,
    ) -> IterationNeed | None:
        """Record a bounded incident and optionally emit a need.

        Incidents still go through rate limits and independent evaluation; the
        shortcut only raises the signal's pressure, never authorizes a change.
        """

        stamp = self._time(now)
        event = ImpulseEvent.create(
            impulse_type=motive,
            intensity=severity,
            source=source,
            source_kind="system",
            context=context,
            persistence=1.0,
            unresolved=True,
            timestamp=_iso_from_epoch(stamp),
            metadata={"incident": _text(reason, 300)},
        )
        accepted = self.accept(event, now=stamp)
        if not accepted:
            # Emergency labels do not bypass the same deduplication/rate
            # boundary as ordinary impulses.  A saturated or replayed source
            # must be inspected by an external supervisor instead.
            return None
        # Incident severity may justify a need before ordinary accumulation
        # reaches the threshold, but still has to be explicitly polled.
        state = self._state(motive)
        if state.pressure < self.threshold and severity >= 0.9:
            state.pressure = max(state.pressure, self.threshold)
        return self.poll_iteration_need(motive, now=stamp, reason=reason, trigger="incident")

    # -- persistence ----------------------------------------------------

    @_synchronized
    def snapshot(self) -> dict[str, Any]:
        self.update()
        payload = {
            "schema_version": MOTIVATION_SCHEMA_VERSION,
            "config": {
                "threshold": self.threshold,
                "release_threshold": self.release_threshold,
                "decay_rate": self.decay_rate,
                "frequency_window": self.frequency_window,
                "frequency_target": self.frequency_target,
                "context_target": self.context_target,
                "unresolved_target": self.unresolved_target,
                "cooldown_seconds": self.cooldown_seconds,
                "rate_cap": self.rate_cap,
                "max_events": self.max_events,
                "self_rate_cap": self.self_rate_cap,
                "self_weight": self.self_weight,
                "self_contribution_cap": self.self_contribution_cap,
                "event_gain": self.event_gain,
                "event_gain_cap": self.event_gain_cap,
                # This policy flag is data, not authority.  A production host
                # may override it on restore; the snapshot can never turn a
                # strict instance into a permissive one implicitly.
                "require_source_attestation": self.require_source_attestation,
            },
            "states": {
                _snapshot_category(key, "motive", limit=80): {
                    **state.to_dict(),
                    "metrics": self._components_for(key, self._last_update),
                }
                for key, state in self._states.items()
            },
            "events": [item.to_dict() for item in self._events],
            "resolved_ids": [
                _snapshot_identifier(item, "event_id", limit=100)
                for item in list(self._resolved_ids)[-self.max_events :]
            ],
            "seen_ids": [
                _snapshot_identifier(item, "event_id", limit=100)
                for item in list(self._seen_ids)[-self._seen_ids.maxlen :]
            ],
            "last_update": self._last_update,
            "total_received": self.total_received,
            "total_accepted": self.total_accepted,
            "total_rejected": self.total_rejected,
            "total_self_rejected": self.total_self_rejected,
            "total_needs_emitted": self.total_needs_emitted,
        }
        payload["integrity_hash"] = _integrity_digest(payload)
        return payload

    def snapshot_for_persistence(self) -> dict[str, Any]:
        """Explicit name for the redacted durable representation."""

        return self.snapshot()

    to_dict = snapshot

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any] | None,
        *,
        clock: Callable[[], float] | None = None,
        source_attestor: MotivationSourceAttestor | None = None,
        source_verifier: Callable[..., bool] | None = None,
        require_source_attestation: bool | None = None,
    ) -> "MotivationalPressure":
        raw_config_hint = (
            data.get("config") if isinstance(data, Mapping) and isinstance(data.get("config"), Mapping) else {}
        )
        effective_required = (
            bool(require_source_attestation)
            if require_source_attestation is not None
            # Binding the concrete HMAC attestor is an explicit production
            # capability.  An arbitrary verifier remains a legacy/offline
            # adapter and must not silently upgrade itself to strict mode.
            # Persisted config is data and cannot turn a bound attestor
            # permissive merely by changing a flag.
            else (
                True
                if source_attestor is not None
                else bool(raw_config_hint.get("require_source_attestation", False))
            )
        )
        if not isinstance(data, Mapping):
            return cls(
                clock=clock,
                source_attestor=source_attestor,
                source_verifier=source_verifier,
                require_source_attestation=effective_required,
            )
        supplied_hash = _text(data.get("integrity_hash", ""), 128).lower()
        if effective_required and not supplied_hash:
            rejected = cls(
                clock=clock,
                source_attestor=source_attestor,
                source_verifier=source_verifier,
                require_source_attestation=effective_required,
            )
            rejected.snapshot_rejected = True
            rejected.restore_error = "integrity_hash_missing"
            return rejected
        if supplied_hash:
            payload = {key: value for key, value in data.items() if key != "integrity_hash"}
            expected_hash = _integrity_digest(payload)
            if supplied_hash != expected_hash:
                rejected = cls(
                    clock=clock,
                    source_attestor=source_attestor,
                    source_verifier=source_verifier,
                    require_source_attestation=effective_required,
                )
                rejected.snapshot_rejected = True
                rejected.restore_error = "integrity_hash_mismatch"
                return rejected
        raw_config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
        # Unknown/malicious config values are clamped by __init__.
        config_kwargs = {
            key: raw_config[key]
            for key in (
                "threshold", "release_threshold", "decay_rate", "frequency_window",
                "frequency_target", "context_target", "unresolved_target",
                "cooldown_seconds", "rate_cap", "max_events", "self_rate_cap",
                "self_weight", "self_contribution_cap", "event_gain", "event_gain_cap",
            )
            if key in raw_config
        }
        config_kwargs["require_source_attestation"] = effective_required
        pressure = cls(
            clock=clock,
            source_attestor=source_attestor,
            source_verifier=source_verifier,
            **config_kwargs,
        )
        raw_states = data.get("states", {})
        if isinstance(raw_states, Mapping):
            for raw_motive, raw_state in list(raw_states.items())[:128]:
                if not isinstance(raw_state, Mapping):
                    continue
                # Snapshot input is untrusted data.  Normalize labels before
                # exposing them through ``motives``/metrics; a legacy payload
                # must not reintroduce arbitrary caller prose after restart.
                safe_motive = _snapshot_category(raw_motive, "motive", limit=80)
                state = pressure._state(safe_motive)
                state.pressure = _number(raw_state.get("pressure", 0.0), 0.0, 0.0, 1.0)
                state.triggered = bool(raw_state.get("triggered", False))
                state.trigger_count = _integer(raw_state.get("trigger_count", 0), 0, 0, 10**9)
                state.last_trigger_at = _number(raw_state.get("last_trigger_at", 0.0), 0.0)
                state.cooldown_until = _number(raw_state.get("cooldown_until", 0.0), 0.0)
                state.last_event_at = _number(raw_state.get("last_event_at", 0.0), 0.0)
                state.event_count = _integer(raw_state.get("event_count", 0), 0, 0, 10**9)
                state.accepted_external = _integer(raw_state.get("accepted_external", 0), 0, 0, 10**9)
                state.accepted_self = _integer(raw_state.get("accepted_self", 0), 0, 0, 10**9)
                state.rejected_self = _integer(raw_state.get("rejected_self", 0), 0, 0, 10**9)
                state.total_intensity = _number(raw_state.get("total_intensity", 0.0), 0.0, 0.0, 10**12)
                state.total_persistence = _number(raw_state.get("total_persistence", 0.0), 0.0, 0.0, 10**12)
        raw_events = data.get("events", [])
        loaded_event_ids: set[str] = set()
        event_id_map: dict[str, str] = {}
        restored_resolved_ids: set[str] = set()
        if isinstance(raw_events, list):
            for raw_item in raw_events[-pressure.max_events :]:
                if not isinstance(raw_item, Mapping):
                    continue
                raw_event_payload = raw_item.get("event", raw_item)
                raw_original_id = ""
                if isinstance(raw_event_payload, Mapping):
                    raw_original_id = _text(
                        raw_event_payload.get(
                            "event_id", raw_event_payload.get("id", "")
                        ),
                        120,
                    ).strip()
                event = ImpulseEvent.from_dict(raw_event_payload)
                if event is None or event.event_id in loaded_event_ids:
                    continue
                pre_snapshot_id = event.event_id
                # Re-serialize through the public redaction boundary before
                # retaining an event restored from external storage.  This
                # removes legacy raw source/context/metadata and deliberately
                # drops any signed provenance supplied by an untrusted
                # snapshot; only a fresh host attestation can authorize a
                # post-restart observation.
                try:
                    safe_event_payload = event.to_dict()
                    safe_event_payload["provenance"] = None
                    event = ImpulseEvent.from_dict(safe_event_payload)
                except Exception:
                    event = None
                if event is None or event.event_id in loaded_event_ids:
                    continue
                if raw_original_id:
                    event_id_map[raw_original_id] = event.event_id
                event_id_map[pre_snapshot_id] = event.event_id
                if not event.unresolved:
                    restored_resolved_ids.add(event.event_id)
                accepted_at = _number(raw_item.get("accepted_at", _epoch(event.timestamp)), pressure._last_update)
                weight = _number(raw_item.get("effective_weight", pressure._source_weight(event)), 0.0, 0.0, 1.0)
                pressure._events.append(_StoredImpulse(event, accepted_at, weight))
                loaded_event_ids.add(event.event_id)
        raw_resolved = data.get("resolved_ids", [])
        if isinstance(raw_resolved, list):
            pressure._resolved_ids = {
                event_id_map.get(
                    _text(item, 120).strip(),
                    _snapshot_identifier(item, "event_id", limit=100),
                )
                for item in raw_resolved[-pressure.max_events :]
                if _text(item, 100)
            }
            pressure._resolved_ids.update(restored_resolved_ids)
        else:
            pressure._resolved_ids = set(restored_resolved_ids)
        raw_seen = data.get("seen_ids", [])
        if isinstance(raw_seen, list):
            for item in raw_seen[-pressure._seen_ids.maxlen :]:
                clean = event_id_map.get(
                    _text(item, 120).strip(),
                    _snapshot_identifier(item, "event_id", limit=100),
                )
                if clean:
                    pressure._remember_id(clean)
        # A legacy snapshot may not carry a replay-guard tail.  In that case,
        # seed it from loaded events in event order; when the tail is present,
        # this idempotent pass preserves the persisted order exactly.
        for item in pressure._events:
            pressure._remember_id(item.event.event_id)
        pressure._last_update = _number(data.get("last_update", pressure._last_update), pressure._last_update)
        pressure.total_received = _integer(data.get("total_received", 0), 0, 0, 10**12)
        pressure.total_accepted = _integer(data.get("total_accepted", len(pressure._events)), len(pressure._events), 0, 10**12)
        pressure.total_rejected = _integer(data.get("total_rejected", 0), 0, 0, 10**12)
        pressure.total_self_rejected = _integer(data.get("total_self_rejected", 0), 0, 0, 10**12)
        pressure.total_needs_emitted = _integer(data.get("total_needs_emitted", 0), 0, 0, 10**12)
        pressure._decay(pressure._last_update)
        return pressure

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, **kwargs: Any) -> "MotivationalPressure":
        return cls.from_snapshot(data, **kwargs)


__all__ = [
    "MOTIVATION_SCHEMA_VERSION",
    "SOURCE_KINDS",
    "PROTECTED_PROPOSAL_SCOPES",
    "ImpulseEvent",
    "MotivationSourceAttestation",
    "MotivationSourceAttestor",
    "MotivationalPressure",
    "IterationNeed",
    "ChangeProposal",
]
