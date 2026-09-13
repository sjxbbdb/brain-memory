"""Bounded, fail-closed projections for public observability surfaces.

The brain keeps richer private state than a dashboard or a local WebSocket
client needs.  This module is deliberately transport-neutral so every public
adapter can apply the same redaction rules without importing the API layer.
"""

from __future__ import annotations

import re
from typing import Any


DROP_KEYS = frozenset(
    {
        "workspace_root",
        "root",
        "source_path",
        "candidate_path",
        "lease_token",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
        "password",
        "credential",
        "ledger",
        "events",
        "audit",
        "audit_log",
        "event_history",
        "records",
        "raw_context",
        "raw_evidence",
        "raw_ledger",
        "raw_proofs",
        "unified_diff",
        "prompt",
        "response",
        "proofs",
        "event_id",
        "replay_key",
        "attestation_hash",
        "authorization_claim_id",
        "authorization_claimed_at",
        "evaluation_execution",
        "execution_metadata",
    }
)

# These fields are useful to a caller's response shape, but their contents
# are capability-bearing.  Keep the key and replace the value uniformly.
REDACT_VALUE_KEYS = frozenset(
    {
        "path",
        "paths",
        "authorization",
        "bearer",
        "signing_key",
        "private_key",
        "candidate",
        "jwt",
    }
)

ABSOLUTE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/]|/(?:Users|home|root|tmp|var|etc|mnt|opt|srv)(?:[\\/]|$))",
    re.IGNORECASE,
)
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?![/\s])[^\r\n\t<>\"']+")
URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9_]{12,}\b|\bsk-[A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:xox[baprs]-|AIza[0-9A-Za-z_-]{10,}|hf_[A-Za-z0-9]{16,}|npm_[A-Za-z0-9]{16,}|"
    r"pypi-[A-Za-z0-9_-]{16,}|lin_api_[A-Za-z0-9_-]{12,}|sq0atp-[A-Za-z0-9_-]{12,})[A-Za-z0-9_-]{8,}\b)",
    re.IGNORECASE,
)
PEM_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|SECRET KEY|CERTIFICATE)-----",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:[A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?(?:KEY|TOKEN)|REFRESH[_-]?TOKEN|SECRET(?:[_-][A-Z0-9_]+)*|PASSWORD|PASSWD|CREDENTIAL|COOKIE|PRIVATE[_-]?KEY|AUTHORIZATION))\s*[:=]\s*\S+",
    re.IGNORECASE,
)
PATH_ASSIGNMENT_RE = re.compile(
    r"\b(?:CANDIDATE|SOURCE|WORKSPACE)[_-]?(?:PATH|ROOT|DIR|FILE)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
AUTH_VALUE_RE = re.compile(r"\b(?:Bearer|Basic|Token)\s+\S+", re.IGNORECASE)
JWT_VALUE_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
CANDIDATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])candidates?[\\/][^\r\n\t<>\"']+",
    re.IGNORECASE,
)
# Relative paths are capability-bearing even when they do not contain a drive
# letter.  Keep this deliberately file-shaped (or rooted in a known runtime
# directory) so ordinary labels such as ``p7-source-static`` remain visible.
RELATIVE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:(?:\.\.?[\\/])|(?:[A-Za-z0-9_.-]+[\\/])){1,8}"
    r"[A-Za-z0-9_.-]+\.(?:py|pyc|json|ya?ml|toml|ini|cfg|db|sqlite3?|log|txt|md|diff|sh|ps1|bat)"
    r"(?=$|[\s,;:)\]}])",
    re.IGNORECASE,
)
KNOWN_RUNTIME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:candidate|active|fixtures?|workspace|brain|storage|services|tools|run)[\\/][^\r\n\t<>\"']+",
    re.IGNORECASE,
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|auth[_-]?token|secret|password|passwd|credential|cookie|private[_-]?key|signing[_-]?key|jwt)(?:$|_)",
    re.IGNORECASE,
)
KEY_PATH_RE = re.compile(
    r"(?:^|[_-])(?:path|root)(?:$|[_-])",
    re.IGNORECASE,
)

MAX_DEPTH = 8
MAX_ITEMS = 64
MAX_STRING = 500


def _normalized_key(key: Any) -> str:
    try:
        text = str(key or "").strip()
        # Treat common camelCase/dotted adapter keys like their snake_case
        # equivalents so ``candidatePath`` cannot bypass the field policy.
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
        text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
        return text.lower().replace("-", "_").replace(".", "_")
    except Exception:
        return ""


def _key_is_unsafe(raw_key: str, normalized: str) -> bool:
    """Reject dynamic keys that can smuggle a path, URI, or secret."""

    return bool(
        URI_RE.search(raw_key)
        or ABSOLUTE_PATH_RE.search(raw_key)
        or POSIX_PATH_RE.search(raw_key)
        or SECRET_ASSIGNMENT_RE.search(raw_key)
        or PATH_ASSIGNMENT_RE.search(raw_key)
        or SECRET_VALUE_RE.search(raw_key)
        or (
            KEY_PATH_RE.search(normalized)
            and normalized not in REDACT_VALUE_KEYS
        )
    )


def sanitize_public_projection(
    value: Any,
    key: str = "",
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Return a bounded JSON-like public view, failing closed on uncertainty."""

    normalized = _normalized_key(key)
    if normalized in DROP_KEYS:
        return None
    if normalized.endswith(("_path", "_root", "_token", "_secret")):
        return None
    if normalized in REDACT_VALUE_KEYS:
        return "<redacted>"
    if SENSITIVE_KEY_RE.search(normalized):
        return None
    if _depth > MAX_DEPTH:
        return None

    if isinstance(value, dict):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        clean: dict[str, Any] = {}
        try:
            for index, (raw_key, raw_value) in enumerate(value.items()):
                if index >= MAX_ITEMS:
                    break
                try:
                    child_key = str(raw_key)[:80]
                except Exception:
                    continue
                child_normalized = _normalized_key(child_key)
                if _key_is_unsafe(child_key, child_normalized):
                    continue
                child = sanitize_public_projection(
                    raw_value,
                    child_key,
                    _depth=_depth + 1,
                    _seen=seen,
                )
                if child is not None:
                    clean[child_key] = child
        except Exception:
            # A malformed adapter object must never make the raw state escape.
            return {}
        finally:
            seen.discard(marker)
        return clean

    if isinstance(value, (list, tuple)):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            return None
        seen.add(marker)
        try:
            return [
                child
                for item in value[:MAX_ITEMS]
                for child in [
                    sanitize_public_projection(
                        item,
                        key,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
                ]
                if child is not None
            ]
        except Exception:
            return []
        finally:
            seen.discard(marker)

    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            if (
                URI_RE.search(value)
                or ABSOLUTE_PATH_RE.search(value)
                or POSIX_PATH_RE.search(value)
                or CANDIDATE_PATH_RE.search(value)
                or RELATIVE_PATH_RE.search(value)
                or KNOWN_RUNTIME_PATH_RE.search(value)
                or SECRET_ASSIGNMENT_RE.search(value)
                or PATH_ASSIGNMENT_RE.search(value)
                or AUTH_VALUE_RE.search(value)
                or JWT_VALUE_RE.search(value)
                or SECRET_VALUE_RE.search(value)
                or PEM_SECRET_RE.search(value)
            ):
                return "<redacted>"
            return value[:MAX_STRING]
        return value
    return None


__all__ = ["sanitize_public_projection"]
