"""Local candidate evolution and promotion boundaries.

This module is intentionally a small, local-only seam around
``brain.evaluation_harness``.  It gives a host a disposable candidate copy,
strict receipt validation, and an append-only hash-chained promotion ledger.
No network, GitHub, credential, or remote-workspace operation is performed.

The boundary is deliberately explicit:

* the evaluator (and its fixture manifest) remains the acceptance authority;
* a rejected candidate is only recorded in the ledger and never copied into
  ``active_root``;
* an accepted candidate may replace ``active_root`` only when the caller passes
  the exact boolean ``authorized=True`` (or an equivalent host authorization
  object) **and** production mode receives a host-issued
  :class:`SandboxAttestation`;
* protected paths are copied from the trusted active tree, never from a
  candidate; a candidate that attempts to alter an existing protected path is
  rejected;
* the previous active tree is retained as a private sibling backup until an
  explicit, authorized rollback or an explicit, authorized discard.

This is a process/filesystem guard, not an OS kernel sandbox.  A candidate
process with host privileges can still make side effects outside its copy;
callers requiring denial of networking or arbitrary host writes must add a
container/job/ACL policy around the evaluator.

``PromotionController`` defaults to the ``production`` profile.  The
``profile="legacy"`` opt-in exists solely for deterministic local tests and
older callers that have not yet wired a host sandbox executor; it must not be
used as a production safety claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Sequence
import uuid

from brain.evaluation_harness import (
    BaselineRevision,
    CandidateRevision,
    EvaluationHarness,
    EvaluationHarnessError,
    EvaluationMode,
    EvaluationReceipt,
    HardGate,
    ResourceBudget,
    directory_fingerprint,
)


EVOLUTION = EvaluationMode.EVOLUTION.value
RECOVERY = EvaluationMode.RECOVERY.value
EVOLUTION_MODE = EVOLUTION
RECOVERY_MODE = RECOVERY
PROMOTION_SCHEMA_VERSION = 1
ATTESTATION_SCHEMA_VERSION = 1
PROMOTION_LEDGER_LOCK_TIMEOUT_SEC = 10.0
# The manifest is a small write-ahead record for the directory swap.  It is
# deliberately independent from the JSONL ledger: the ledger proves what was
# accepted, while the manifest tells a fresh controller how to reconcile a
# process that disappeared between two filesystem operations.
PROMOTION_MANIFEST_SCHEMA_VERSION = 1
PROMOTION_MANIFEST_MAX_BYTES = 128 * 1024
SENSITIVE_SCAN_MAX_ENTRIES = 100_000
SENSITIVE_SCAN_MAX_DEPTH = 64
SANDBOX_ATTESTATION_MAX_TTL_SEC = 60 * 60
REQUIRED_SANDBOX_CAPABILITIES = frozenset(
    {
        "filesystem_scope",
        "network_denied",
        "process_tree_kill",
        "resource_limits",
    }
)


class EvolutionError(RuntimeError):
    """Base error for the local evolution boundary."""


class PromotionError(EvolutionError):
    """The candidate, receipt, or promotion state is invalid."""


class ReceiptValidationError(PromotionError):
    """A fixed evaluator receipt cannot be accepted as promotion evidence."""


class AuthorizationError(PromotionError):
    """A host did not explicitly authorize an active-tree write."""


class ProtectedFileError(PromotionError):
    """A candidate attempted to alter a protected path."""


class CandidateIsolationError(PromotionError):
    """A candidate cannot be materialized without aliasing live state."""


class LedgerError(PromotionError):
    """The append-only promotion ledger is invalid or unavailable."""


class SandboxAttestationError(PromotionError):
    """A production promotion lacks a valid host-issued sandbox proof."""


# Compatibility aliases used by a few orchestration callers.
PromotionAuthorizationError = AuthorizationError
EvaluationReceiptError = ReceiptValidationError
ProtectedPathError = ProtectedFileError
EvolutionBoundaryError = EvolutionError
HostSandboxAttestationError = SandboxAttestationError


class PromotionMode(str, Enum):
    """The only two reasons a candidate can be considered for promotion."""

    EVOLUTION = EVOLUTION
    RECOVERY = RECOVERY

    @classmethod
    def parse(cls, value: "PromotionMode | EvaluationMode | str") -> "PromotionMode":
        if isinstance(value, cls):
            return value
        if isinstance(value, EvaluationMode):
            return cls(value.value)
        text = str(value or "").strip().lower()
        aliases = {"evolve": cls.EVOLUTION, "repair": cls.RECOVERY, "restore": cls.RECOVERY}
        if text in aliases:
            return aliases[text]
        try:
            return cls(text)
        except ValueError as exc:
            raise PromotionError(f"unknown promotion mode: {value!r}") from exc


# Several names make the seam readable to code that already uses the harness
# vocabulary.  They are aliases, not separate mode implementations.
EvolutionMode = PromotionMode
PromotionType = PromotionMode


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_text(value: Any, limit: int = 1000) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch if ord(ch) >= 32 or ch in "\r\n\t" else " " for ch in text)
    return text[:limit]


def _freeze(value: Any, depth: int = 0) -> Any:
    """Freeze small ledger metadata into JSON-safe immutable values."""

    if depth > 5:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, str)):
        return _bounded_text(value, 500) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        return {
            _bounded_text(key, 120): _freeze(item, depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple, set)):
        values = sorted(value, key=str) if isinstance(value, set) else value
        return [_freeze(item, depth + 1) for item in list(values)[:64]]
    return _bounded_text(value, 500)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SENSITIVE_NAME_RE = re.compile(
    r"(^|[._-])(env|key|token|secret|password|passwd|credential|cookie|auth)([._-]|$)",
    re.IGNORECASE,
)
_SENSITIVE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "secrets",
    "credentials",
    "__secrets__",
}
_EXTERNAL_MANAGED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
    }
)
# SQLite and similar journal files are mutable continuity state, not part of
# an atomically swappable program tree.  Keeping these names outside the
# active/candidate trees prevents a successful promotion from silently
# replacing the life/memory database with the candidate's copy (or with
# nothing when the candidate omits it).  The check is metadata-only; the
# persistence owner remains responsible for opening the external path.
_MUTABLE_STATE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
)


def _is_sensitive_name(name: str) -> bool:
    base = Path(name).name.lower()
    # Public templates contain no runtime secret and should survive a whole
    # tree promotion.  Treating ``.env.example`` as a credential would make
    # the evaluator silently drop a useful, non-sensitive project artifact.
    if base in {".env.example", ".env.sample", ".env.template"}:
        return False
    if base in {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "credential",
        "apikey",
        "api_key",
        "private_key",
    }:
        return True
    return bool(_SENSITIVE_NAME_RE.search(base) or re.search(r"api[_-]?key", base, re.IGNORECASE))


_SENSITIVE_METADATA_PARTS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "token",
        "apikey",
        "accesskey",
        "privatekey",
        "signingkey",
        "clientsecret",
        "authorization",
        "cookie",
        "jwt",
        "bearer",
        "session",
    }
)


def _sensitive_metadata_key(raw_key: Any) -> bool:
    text = str(raw_key or "")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", text) if part]
    compact = "".join(parts)
    if compact in _SENSITIVE_METADATA_PARTS:
        return True
    if any(part in _SENSITIVE_METADATA_PARTS for part in parts):
        return True
    return bool(
        any(
            left in parts and right in parts
            for left, right in (("api", "key"), ("access", "token"), ("refresh", "token"), ("private", "key"), ("signing", "key"), ("client", "secret"), ("auth", "token"))
        )
    )


def _contains_sensitive_metadata(value: Any, *, depth: int = 0) -> bool:
    """Reject credential-shaped metadata keys before they become permanent."""

    if depth > 6:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _sensitive_metadata_key(key) or _contains_sensitive_metadata(item, depth=depth + 1):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_contains_sensitive_metadata(item, depth=depth + 1) for item in value)
    return False


def _is_link_like(path: Path) -> bool:
    """Return whether *path* is a symlink or Windows reparse/junction point.

    ``Path.is_symlink()`` does not identify every Windows reparse point (most
    notably directory junctions).  Treating those entries as ordinary local
    directories would let a candidate or a recovery cleanup cross the active
    root boundary.  The metadata check never opens or traverses the target.
    """

    try:
        if path.is_symlink():
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if os.name != "nt":
        return False
    try:
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        stat_result = path.stat(follow_symlinks=False)
        # FILE_ATTRIBUTE_REPARSE_POINT.  ``st_file_attributes`` is exposed by
        # Python's Windows stat implementation.  If a non-standard runtime
        # omits it, fail closed instead of treating an uninspectable entry as
        # an ordinary directory.
        attributes = getattr(stat_result, "st_file_attributes", None)
        if attributes is None:
            return True
        return bool(int(attributes) & 0x400)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        # An entry that cannot be inspected is unsafe for a boundary check.
        return True


def _lexists(path: Path) -> bool:
    """Existence check that also sees dangling symlinks/reparse points."""

    try:
        return bool(os.path.lexists(str(path)))
    except (OSError, ValueError, TypeError):
        return True


def _external_managed_entries(root: Path) -> tuple[str, ...]:
    """List top-level VCS/dependency trees without opening their contents.

    Whole-directory promotion cannot safely preserve these trees without
    reading or copying opaque metadata (for example ``.git/config``).  They
    therefore belong to the host/runtime boundary, not the atomically swapped
    organism tree.
    """

    if _is_link_like(root) or not root.is_dir():
        raise CandidateIsolationError(f"expected a real local directory: {root}")
    found: list[str] = []
    for name in sorted(_EXTERNAL_MANAGED_DIRS):
        path = root / name
        if _lexists(path):
            found.append(name)
    return tuple(found)


def _mutable_state_entries(root: Path) -> tuple[str, ...]:
    """Find database/journal names that must live outside a swap tree.

    This intentionally examines directory and file names only.  It skips
    externally managed VCS/dependency trees and uses the same bounded walk as
    the sensitive-path guard, so a controller never opens a database merely
    to decide whether it is safe to swap the surrounding directory.
    """

    if _is_link_like(root) or not root.is_dir():
        raise CandidateIsolationError(f"expected a real local directory: {root}")
    found: list[str] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    inspected = 0
    generated_dirs = {".git", ".hg", ".svn", ".venv", "venv", "node_modules"}
    while pending:
        current, depth = pending.pop()
        if depth > SENSITIVE_SCAN_MAX_DEPTH:
            raise CandidateIsolationError("mutable-state scan exceeded its depth limit")
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            raise CandidateIsolationError(f"cannot inspect directory: {current}") from exc
        for entry in entries:
            inspected += 1
            if inspected > SENSITIVE_SCAN_MAX_ENTRIES:
                raise CandidateIsolationError("mutable-state scan exceeded its entry limit")
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if _is_link_like(path):
                raise CandidateIsolationError(f"link-like entry is not allowed: {rel}")
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise CandidateIsolationError(f"cannot inspect entry: {rel}") from exc
            if is_dir:
                if entry.name.lower() not in generated_dirs:
                    pending.append((path, depth + 1))
                continue
            if is_file and any(entry.name.lower().endswith(suffix) for suffix in _MUTABLE_STATE_SUFFIXES):
                found.append(rel)
            elif not is_file:
                raise CandidateIsolationError(f"unsupported filesystem entry: {rel}")
    return tuple(sorted(found))


def _assert_no_reparse_components(value: str | os.PathLike[str]) -> None:
    """Reject a raw input path whose existing component is link-like.

    Checking only the result of ``Path.resolve()`` loses evidence when the
    input itself is a junction to another tree.  Walk the lexical components
    first and inspect metadata without following them; missing final
    components are allowed for callers creating a sidecar later.
    """

    raw_path = Path(value)
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    try:
        parts = raw_path.parts
        if not parts:
            return
        current = Path(parts[0])
        if _is_link_like(current):
            raise PromotionError("reparse-point path components are outside the local contract")
        for part in parts[1:]:
            current = current / part
            if _is_link_like(current):
                raise PromotionError("reparse-point path components are outside the local contract")
            if not current.exists():
                break
    except PromotionError:
        raise
    except (OSError, ValueError) as exc:
        raise PromotionError("cannot inspect local path components") from exc


def _resolve_local(value: str | os.PathLike[str], *, strict: bool = True) -> Path:
    raw = os.fsdecode(os.fspath(value))
    if _URL_RE.match(raw) or raw.startswith(("\\\\", "//")):
        raise PromotionError("network and URL paths are outside the local evolution contract")
    try:
        _assert_no_reparse_components(value)
        return Path(value).resolve(strict=strict)
    except OSError as exc:
        raise PromotionError(f"cannot resolve local path: {value!r}") from exc


@contextmanager
def _promotion_ledger_file_lock(
    ledger_path: Path,
    *,
    timeout_sec: float = PROMOTION_LEDGER_LOCK_TIMEOUT_SEC,
):
    """Hold an inter-process advisory lock for one JSONL ledger operation.

    ``threading.RLock`` protects only callers in this Python process.  A
    promotion ledger can also be opened by a recovery worker or a second host
    process, so append/verify must serialize at the filesystem boundary.  A
    small sidecar is used rather than locking the data file itself: readers
    never inherit a platform-specific share-mode restriction, and a crashed
    process releases the OS advisory lock automatically.  This is a
    coordination guard, not an ACL or a cryptographic trust boundary.
    """

    lock_path = Path(str(ledger_path) + ".lock")
    try:
        _assert_no_reparse_components(lock_path)
    except PromotionError as exc:
        raise LedgerError("promotion lock path contains a reparse component") from exc
    if _is_link_like(lock_path):
        raise LedgerError("promotion lock sidecar cannot be a symlink/junction")
    if _lexists(lock_path):
        try:
            if not lock_path.is_file():
                raise LedgerError("promotion lock sidecar must be a regular file")
            lock_stat = lock_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise LedgerError("cannot inspect promotion lock sidecar") from exc
        if getattr(lock_stat, "st_nlink", 1) > 1:
            raise LedgerError("promotion lock sidecar hard links are not supported")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep one byte in the sidecar so Windows ``msvcrt.locking`` has a
        # stable region to lock even on the first operation.
        stream = lock_path.open("a+b")
        try:
            stream_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(stream_stat.st_mode) or getattr(stream_stat, "st_nlink", 1) > 1:
                stream.close()
                raise LedgerError("promotion lock sidecar is not a single regular file")
        except OSError as exc:
            try:
                stream.close()
            except OSError:
                pass
            raise LedgerError("cannot inspect opened promotion lock sidecar") from exc
    except OSError as exc:
        raise LedgerError(f"cannot open promotion ledger lock: {lock_path}") from exc

    acquired = False
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LedgerError(
                            f"timed out waiting for promotion ledger lock: {lock_path}"
                        )
                    time.sleep(0.02)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LedgerError(
                            f"timed out waiting for promotion ledger lock: {lock_path}"
                        )
                    time.sleep(0.02)
        yield stream
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor still releases the advisory lock;
                # never mask the operation's original result with cleanup
                # noise.
                pass
        try:
            stream.close()
        except OSError:
            pass


def _assert_single_link_file(path: Path) -> None:
    """Reject hard-linked ledger files whose aliases cannot share a sidecar lock.

    The JSONL ledger uses a path-based advisory sidecar.  Two hard-link names
    can address the same inode while producing different sidecars, allowing
    concurrent writers to fork the hash chain.  A hard link is therefore
    outside this persistence contract; callers should use one canonical path
    (or an external serialized store) instead.
    """

    try:
        if _is_link_like(path):
            raise LedgerError("promotion ledger symlinks are not supported")
        if not path.is_file():
            raise LedgerError("promotion ledger must be a regular file")
        stat_result = path.stat()
    except OSError as exc:
        raise LedgerError(f"cannot inspect promotion ledger: {path}") from exc
    if getattr(stat_result, "st_nlink", 1) > 1:
        raise LedgerError("promotion ledger hard links are not supported")


def _normalise_rel(value: str | os.PathLike[str]) -> str:
    raw = os.fsdecode(os.fspath(value)).replace("\\", "/").strip()
    if not raw or raw in {".", "/"}:
        raise PromotionError("protected path must be a non-empty relative path")
    path = PurePosixPath(raw)
    # ``PurePosixPath`` alone treats ``C:/secret`` as a relative path on
    # every host.  The normalized value is later joined with ``active_root``
    # using the native ``Path`` class, where that same string is an absolute
    # drive path on Windows.  Reject both POSIX and Windows rooted/drive
    # forms before any path is opened or copied.
    windows_path = PureWindowsPath(raw)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or raw.startswith(("//", "\\\\"))
        or ".." in path.parts
    ):
        raise PromotionError(f"protected path must stay relative: {value!r}")
    # A path ending in a slash is represented without the slash, which makes
    # file and directory-prefix matching deterministic.
    return "/".join(part for part in path.parts if part not in {"", "."})


def _iter_files(
    root: Path,
    *,
    budget: ResourceBudget | None = None,
) -> Iterable[tuple[Path, str]]:
    """Yield regular, non-sensitive files without following links."""

    if _is_link_like(root) or not root.is_dir():
        raise CandidateIsolationError(f"expected a real local directory: {root}")
    rows: list[tuple[Path, str]] = []
    file_count = 0
    total_bytes = 0
    inspected_entries = 0
    max_entries = max(1024, int(budget.max_files) * 8) if budget is not None else 100_000
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for raw_dir in dirs:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise CandidateIsolationError("candidate entry count exceeds the promotion budget")
            path = current_path / raw_dir
            if _is_link_like(path):
                raise CandidateIsolationError(f"symlink directory is not allowed: {path}")
            if raw_dir.lower() in _SENSITIVE_DIRS:
                continue
            safe_dirs.append(raw_dir)
        dirs[:] = safe_dirs
        for raw_file in files:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise CandidateIsolationError("candidate entry count exceeds the promotion budget")
            path = current_path / raw_file
            rel = path.relative_to(root).as_posix()
            if _is_link_like(path):
                raise CandidateIsolationError(f"symlink/reparse file is not allowed: {path}")
            if _is_sensitive_name(rel):
                continue
            try:
                stat_result = path.stat()
                mode = stat_result.st_mode
            except OSError as exc:
                raise CandidateIsolationError(f"cannot stat file: {path}") from exc
            if not stat.S_ISREG(mode):
                raise CandidateIsolationError(f"non-regular file is not allowed: {path}")
            # A hard link can point outside the candidate tree (including to
            # a credential file).  Copying it would read host-owned bytes
            # while the path still looks like an ordinary candidate file.
            # Reject links with more than one directory entry; normal files
            # on the supported filesystems report ``st_nlink == 1``.
            if getattr(stat_result, "st_nlink", 1) > 1:
                raise CandidateIsolationError(f"hard-linked file is not allowed: {path}")
            size = max(0, int(stat_result.st_size))
            if budget is not None:
                if file_count >= budget.max_files:
                    raise CandidateIsolationError("candidate file count exceeds the promotion budget")
                if size > budget.max_file_bytes:
                    raise CandidateIsolationError(
                        f"candidate file exceeds max_file_bytes: {path}"
                    )
                if total_bytes + size > budget.max_total_bytes:
                    raise CandidateIsolationError("candidate bytes exceed the promotion budget")
            file_count += 1
            total_bytes += size
            rows.append((path, rel))
    rows.sort(key=lambda item: item[1])
    return rows


def _sensitive_entries(root: Path) -> tuple[str, ...]:
    """List credential-shaped entries by metadata only, never by contents.

    The promotion operation replaces the active directory as a whole.  The
    normal copy walk intentionally omits private-looking entries so an
    evaluator cannot read them; that omission is unsafe for an already-live
    tree because an unprotected ``runtime/.env`` would silently disappear.
    We therefore reject such trees before evaluation/promotion.  Generated
    directories (VCS metadata, virtual environments and dependency caches)
    remain externally managed and are skipped; explicit secret stores are
    still reported as unsafe.
    """

    if _is_link_like(root) or not root.is_dir():
        raise CandidateIsolationError(f"expected a real local directory: {root}")
    found: list[str] = []
    generated_dirs = {".git", ".hg", ".svn", ".venv", "venv", "node_modules"}
    secret_dirs = {"secrets", "credentials", "__secrets__"}
    pending: list[tuple[Path, int]] = [(root, 0)]
    inspected = 0
    while pending:
        current, depth = pending.pop()
        if depth > SENSITIVE_SCAN_MAX_DEPTH:
            raise CandidateIsolationError("sensitive-path scan exceeded its depth limit")
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            raise CandidateIsolationError(f"cannot inspect directory: {current}") from exc
        for entry in entries:
            inspected += 1
            if inspected > SENSITIVE_SCAN_MAX_ENTRIES:
                raise CandidateIsolationError("sensitive-path scan exceeded its entry limit")
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if _is_link_like(path):
                raise CandidateIsolationError(f"link-like entry is not allowed: {rel}")
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise CandidateIsolationError(f"cannot inspect entry: {rel}") from exc
            if is_dir:
                lowered = entry.name.lower()
                if lowered in secret_dirs:
                    found.append(rel)
                    continue
                if lowered in generated_dirs:
                    continue
                pending.append((path, depth + 1))
                continue
            if is_file:
                if _is_sensitive_name(rel):
                    found.append(rel)
                continue
            raise CandidateIsolationError(f"unsupported filesystem entry: {rel}")
    return tuple(found)


def _sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    try:
        stat_result = path.stat()
        if getattr(stat_result, "st_nlink", 1) > 1:
            raise ProtectedFileError(f"hard-linked file is not allowed: {path}")
        if max_bytes is not None and int(stat_result.st_size) > max_bytes:
            raise ProtectedFileError(f"protected file exceeds max_file_bytes: {path}")
        read_bytes = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                read_bytes += len(chunk)
                if max_bytes is not None and read_bytes > max_bytes:
                    raise ProtectedFileError(f"protected file exceeds max_file_bytes: {path}")
                digest.update(chunk)
    except OSError as exc:
        raise PromotionError(f"cannot read protected file: {path}") from exc
    return digest.hexdigest()


_MISSING = "<missing>"


def _path_digest(
    root: Path,
    relative: str,
    *,
    budget: ResourceBudget | None = None,
) -> str:
    target = root / Path(relative)
    if _is_link_like(target):
        raise ProtectedFileError(f"symlink protected path is not allowed: {target}")
    if not target.exists():
        return _MISSING
    if target.is_dir():
        _validate_protected_directory(target, budget=budget)
        return directory_fingerprint(target, budget=budget)
    if target.is_file():
        return _sha256_file(
            target,
            max_bytes=budget.max_file_bytes if budget is not None else None,
        )
    raise ProtectedFileError(f"unsupported protected path: {target}")


def _validate_protected_directory(
    root: Path,
    *,
    budget: ResourceBudget | None = None,
) -> None:
    """Validate a protected directory using names/metadata only.

    Fingerprint/copy helpers intentionally skip credential-looking files so a
    candidate cannot make the evaluator read them.  That is safe only when a
    protected directory contains no such descendant: otherwise the skipped
    file could disappear during staging while the digest still appears equal.
    Reject the ambiguous configuration before any protected bytes are opened.
    """

    if _is_link_like(root) or not root.is_dir():
        raise ProtectedFileError(f"protected path is not a real directory: {root}")
    file_count = 0
    total_bytes = 0
    inspected_entries = 0
    max_entries = max(1024, int(budget.max_files) * 8) if budget is not None else 100_000
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for raw_dir in dirs:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise ProtectedFileError("protected directory exceeds the inspection budget")
            child = current_path / raw_dir
            if _is_link_like(child):
                raise ProtectedFileError(f"symlink protected path is not allowed: {child}")
            if _is_sensitive_name(raw_dir) or raw_dir.lower() in _SENSITIVE_DIRS:
                raise ProtectedFileError(
                    f"protected directory contains a sensitive descendant: {child}"
                )
            safe_dirs.append(raw_dir)
        dirs[:] = safe_dirs
        for raw_file in files:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise ProtectedFileError("protected directory exceeds the inspection budget")
            child = current_path / raw_file
            if _is_link_like(child):
                raise ProtectedFileError(f"symlink protected path is not allowed: {child}")
            if _is_sensitive_name(raw_file):
                raise ProtectedFileError(
                    f"protected directory contains a sensitive descendant: {child}"
                )
            try:
                stat_result = child.stat()
            except OSError as exc:
                raise ProtectedFileError(f"cannot inspect protected path: {child}") from exc
            if not stat.S_ISREG(stat_result.st_mode):
                raise ProtectedFileError(f"unsupported protected path: {child}")
            if getattr(stat_result, "st_nlink", 1) > 1:
                raise ProtectedFileError(f"hard-linked protected file is not allowed: {child}")
            file_count += 1
            total_bytes += max(0, int(stat_result.st_size))
            if budget is not None and (
                file_count > budget.max_files
                or int(stat_result.st_size) > budget.max_file_bytes
                or total_bytes > budget.max_total_bytes
            ):
                raise ProtectedFileError("protected directory exceeds the promotion budget")


def _is_under(relative: str, protected: str) -> bool:
    return relative == protected or relative.startswith(protected + "/")


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> int:
    """Copy one regular file after an fd-level alias/symlink check.

    The initial directory walk is only a snapshot.  Opening the source by
    descriptor and checking ``fstat`` closes the simple check-then-swap race
    where a candidate replaces a path with a symlink or hard link between the
    walk and ``shutil.copy2``.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or _is_link_like(destination):
        raise CandidateIsolationError(f"destination already exists: {destination}")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    copied_bytes = 0
    try:
        fd = os.open(str(source), flags)
    except OSError as exc:
        raise CandidateIsolationError(f"cannot open candidate file: {source}") from exc
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise CandidateIsolationError(f"non-regular file is not allowed: {source}")
        if getattr(stat_result, "st_nlink", 1) > 1:
            raise CandidateIsolationError(f"hard-linked file is not allowed: {source}")
        if max_bytes is not None and int(stat_result.st_size) > max_bytes:
            raise CandidateIsolationError(f"candidate file exceeds max_file_bytes: {source}")
        with os.fdopen(fd, "rb", closefd=True) as source_stream:
            fd = -1
            with destination.open("xb") as destination_stream:
                while True:
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if max_bytes is not None and copied_bytes + len(chunk) > max_bytes:
                        raise CandidateIsolationError(
                            f"candidate file exceeds max_file_bytes: {source}"
                        )
                    destination_stream.write(chunk)
                    copied_bytes += len(chunk)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
    except CandidateIsolationError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if _lexists(destination) and not _is_link_like(destination):
                destination.unlink()
        except OSError:
            pass
        raise
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise CandidateIsolationError(f"cannot copy candidate file: {source}") from exc
    return copied_bytes


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    exclude: Sequence[str] = (),
    budget: ResourceBudget | None = None,
    usage: list[int] | None = None,
) -> None:
    if _is_link_like(destination) or _lexists(destination):
        raise CandidateIsolationError(f"destination already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    excludes = tuple(exclude)
    if usage is None:
        usage = [0, 0]
    for source_file, relative in _iter_files(source, budget=budget):
        if any(_is_under(relative, item) for item in excludes):
            continue
        try:
            expected_size = max(0, int(source_file.stat().st_size))
        except OSError as exc:
            raise CandidateIsolationError(f"cannot stat candidate file: {source_file}") from exc
        if budget is not None:
            if usage[0] >= budget.max_files:
                raise CandidateIsolationError("candidate file count exceeds the promotion budget")
            if expected_size > budget.max_file_bytes:
                raise CandidateIsolationError(
                    f"candidate file exceeds max_file_bytes: {source_file}"
                )
            if usage[1] + expected_size > budget.max_total_bytes:
                raise CandidateIsolationError("candidate bytes exceed the promotion budget")
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = _copy_regular_file(
            source_file,
            target,
            max_bytes=budget.max_file_bytes if budget is not None else None,
        )
        usage[0] += 1
        usage[1] += copied
        if budget is not None and usage[1] > budget.max_total_bytes:
            raise CandidateIsolationError("candidate bytes exceed the promotion budget")


def _copy_protected(
    source_root: Path,
    destination_root: Path,
    protected: Sequence[str],
    *,
    budget: ResourceBudget | None = None,
    usage: list[int] | None = None,
) -> None:
    if usage is None:
        usage = [0, 0]
    for relative in protected:
        source = source_root / Path(relative)
        if _is_link_like(source):
            raise ProtectedFileError(f"symlink protected path is not allowed: {source}")
        if not _lexists(source):
            continue
        target = destination_root / Path(relative)
        if source.is_dir():
            _validate_protected_directory(source, budget=budget)
            target.mkdir(parents=True, exist_ok=True)
            for source_file, child_relative in _iter_files(source, budget=budget):
                try:
                    expected_size = max(0, int(source_file.stat().st_size))
                except OSError as exc:
                    raise ProtectedFileError(
                        f"cannot stat protected path: {source_file}"
                    ) from exc
                if budget is not None:
                    if usage[0] >= budget.max_files:
                        raise ProtectedFileError("protected paths exceed the promotion file budget")
                    if expected_size > budget.max_file_bytes:
                        raise ProtectedFileError(
                            f"protected file exceeds max_file_bytes: {source_file}"
                        )
                    if usage[1] + expected_size > budget.max_total_bytes:
                        raise ProtectedFileError("protected paths exceed the promotion byte budget")
                child_target = target / Path(child_relative)
                child_target.parent.mkdir(parents=True, exist_ok=True)
                copied = _copy_regular_file(
                    source_file,
                    child_target,
                    max_bytes=budget.max_file_bytes if budget is not None else None,
                )
                usage[0] += 1
                usage[1] += copied
        elif source.is_file():
            try:
                expected_size = max(0, int(source.stat().st_size))
            except OSError as exc:
                raise ProtectedFileError(f"cannot stat protected path: {source}") from exc
            if budget is not None:
                if usage[0] >= budget.max_files:
                    raise ProtectedFileError("protected paths exceed the promotion file budget")
                if expected_size > budget.max_file_bytes:
                    raise ProtectedFileError(
                        f"protected file exceeds max_file_bytes: {source}"
                    )
                if usage[1] + expected_size > budget.max_total_bytes:
                    raise ProtectedFileError("protected paths exceed the promotion byte budget")
            target.parent.mkdir(parents=True, exist_ok=True)
            copied = _copy_regular_file(
                source,
                target,
                max_bytes=budget.max_file_bytes if budget is not None else None,
            )
            usage[0] += 1
            usage[1] += copied
        else:
            raise ProtectedFileError(f"unsupported protected path: {source}")


def _safe_owned_remove(path: Path | None, *, parent: Path | None = None) -> None:
    """Remove only a temporary path created by this module."""

    if path is None:
        return
    if _is_link_like(path):
        raise PromotionError("refusing to remove a symlink/junction transaction path")
    if not _lexists(path):
        return
    if parent is not None:
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError:
            return
    if not path.name.startswith(".brain-memory-"):
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


_MANIFEST_PHASE_IDLE = "IDLE"
_MANIFEST_PHASE_PREPARED = "PREPARED"
_MANIFEST_PHASE_ACTIVE_MOVED = "ACTIVE_MOVED"
_MANIFEST_PHASE_SWAPPED = "SWAPPED"
_MANIFEST_PHASE_COMMITTED = "COMMITTED"
_MANIFEST_PHASE_ROLLBACK_STARTED = "ROLLBACK_STARTED"
_MANIFEST_PHASE_ROLLED_BACK = "ROLLED_BACK"
_MANIFEST_PHASE_FINALIZING = "FINALIZING"
_MANIFEST_PHASES = frozenset(
    {
        _MANIFEST_PHASE_IDLE,
        _MANIFEST_PHASE_PREPARED,
        _MANIFEST_PHASE_ACTIVE_MOVED,
        _MANIFEST_PHASE_SWAPPED,
        _MANIFEST_PHASE_COMMITTED,
        _MANIFEST_PHASE_ROLLBACK_STARTED,
        _MANIFEST_PHASE_ROLLED_BACK,
        _MANIFEST_PHASE_FINALIZING,
    }
)
# Public, stable spellings for hosts that want to display or audit the
# transaction state without depending on private implementation names.
PROMOTION_MANIFEST_PHASE_IDLE = _MANIFEST_PHASE_IDLE
PROMOTION_MANIFEST_PHASE_PREPARED = _MANIFEST_PHASE_PREPARED
PROMOTION_MANIFEST_PHASE_ACTIVE_MOVED = _MANIFEST_PHASE_ACTIVE_MOVED
PROMOTION_MANIFEST_PHASE_SWAPPED = _MANIFEST_PHASE_SWAPPED
PROMOTION_MANIFEST_PHASE_COMMITTED = _MANIFEST_PHASE_COMMITTED
PROMOTION_MANIFEST_PHASE_ROLLBACK_STARTED = _MANIFEST_PHASE_ROLLBACK_STARTED
PROMOTION_MANIFEST_PHASE_ROLLED_BACK = _MANIFEST_PHASE_ROLLED_BACK
PROMOTION_MANIFEST_PHASE_FINALIZING = _MANIFEST_PHASE_FINALIZING
PROMOTION_MANIFEST_PHASES = frozenset(_MANIFEST_PHASES)
_MANIFEST_NAME_RE = re.compile(
    r"^\.brain-memory-(?:backup|rollback-current)-[0-9a-f]{32}$"
)
_MANIFEST_STAGE_NAME_RE = re.compile(r"^\.brain-memory-stage-[A-Za-z0-9_-]{1,80}$")


def _root_identity_digest(root: Path) -> str:
    """Return a stable, non-secret identity for one canonical active root."""

    identity = os.path.normcase(str(root))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _manifest_path_for(root: Path) -> Path:
    return root.parent / f".brain-memory-controller-{_root_identity_digest(root)}.manifest.json"


def _manifest_payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionManifest:
    """Write-ahead state for one active-tree transaction.

    Only sibling *basenames* are persisted.  A fresh controller reconstructs
    them under the canonical active-root parent and rejects traversal,
    symlink, hard-link, or foreign-root values before touching the filesystem.
    The content hash detects torn/partial writes; it is not intended as an
    authentication mechanism.
    """

    root_digest: str
    phase: str
    operation_id: str = ""
    backup_name: str | None = None
    stage_name: str | None = None
    # ``candidate_fingerprint`` identifies the source tree that the evaluator
    # judged.  The staged tree is not byte-for-byte identical because trusted
    # protected paths are overlaid from the active tree, so it needs its own
    # durable fingerprint for crash-time cleanup.  It is optional when reading
    # manifests written by the pre-fingerprint implementation; those legacy
    # records are handled fail-closed whenever a stage would be removed.
    stage_fingerprint: str | None = None
    current_name: str | None = None
    before_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    after_fingerprint: str | None = None
    receipt_hash: str | None = None
    event_hash: str | None = None
    mode: str | None = None
    updated_at: str = ""
    manifest_hash: str = ""

    def __post_init__(self) -> None:
        root_digest = _bounded_text(self.root_digest, 64).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{24}", root_digest):
            raise PromotionError("promotion manifest root identity is invalid")
        phase = _bounded_text(self.phase, 40).strip().upper()
        if phase not in _MANIFEST_PHASES:
            raise PromotionError("promotion manifest phase is invalid")
        operation_id = _bounded_text(self.operation_id, 80).strip()
        if phase != _MANIFEST_PHASE_IDLE and not operation_id:
            raise PromotionError("non-idle promotion manifest requires an operation id")
        if operation_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", operation_id):
            raise PromotionError("promotion manifest operation id is invalid")
        for name in ("backup_name", "stage_name", "current_name"):
            value = _bounded_text(getattr(self, name), 120).strip() or None
            valid_name = (
                _MANIFEST_STAGE_NAME_RE.fullmatch(value or "")
                if name == "stage_name"
                else _MANIFEST_NAME_RE.fullmatch(value or "")
            )
            if value is not None and (value != Path(value).name or not valid_name):
                raise PromotionError(f"promotion manifest {name} is invalid")
            object.__setattr__(self, name, value)
        sibling_names = [
            value
            for value in (self.backup_name, self.stage_name, self.current_name)
            if value is not None
        ]
        if len(sibling_names) != len(set(sibling_names)):
            raise PromotionError("promotion manifest sibling names must be distinct")
        for name in (
            "before_fingerprint",
            "candidate_fingerprint",
            "stage_fingerprint",
            "after_fingerprint",
            "receipt_hash",
            "event_hash",
        ):
            value = _bounded_text(getattr(self, name), 128).strip().lower() or None
            if value is not None and not _is_sha256(value):
                raise PromotionError(f"promotion manifest {name} is invalid")
            object.__setattr__(self, name, value)
        mode = _bounded_text(self.mode, 40).strip().lower() or None
        if mode is not None:
            # Parse aliases as well as the canonical values, but persist only
            # the canonical spelling.
            mode = PromotionMode.parse(mode).value
        updated_at = _bounded_text(self.updated_at, 100).strip() or _utc_now()
        object.__setattr__(self, "root_digest", root_digest)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "updated_at", updated_at)
        if phase == _MANIFEST_PHASE_IDLE and any(
            (
                operation_id,
                self.backup_name,
                self.stage_name,
                self.stage_fingerprint,
                self.current_name,
                self.before_fingerprint,
                self.candidate_fingerprint,
                self.after_fingerprint,
                self.receipt_hash,
                self.event_hash,
                mode,
            )
        ):
            raise PromotionError("idle promotion manifest must not retain transaction data")
        # Keep the durable state machine explicit.  These checks reject a
        # self-consistent-but-impossible JSON record before any recovery code
        # is allowed to touch a sibling directory.  Transitional current
        # names are permitted for PREPARED/ACTIVE_MOVED/SWAPPED because a
        # recovery worker may have persisted one while creating a retirement
        # path.
        required: dict[str, tuple[str, ...]] = {
            _MANIFEST_PHASE_PREPARED: (
                "backup_name", "stage_name", "before_fingerprint",
                "candidate_fingerprint", "receipt_hash", "mode",
            ),
            _MANIFEST_PHASE_ACTIVE_MOVED: (
                "backup_name", "stage_name", "before_fingerprint",
                "candidate_fingerprint", "receipt_hash", "mode",
            ),
            _MANIFEST_PHASE_SWAPPED: (
                "backup_name", "before_fingerprint", "candidate_fingerprint",
                "after_fingerprint", "receipt_hash", "mode",
            ),
            _MANIFEST_PHASE_COMMITTED: (
                "backup_name", "before_fingerprint", "candidate_fingerprint",
                "after_fingerprint", "receipt_hash", "event_hash", "mode",
            ),
            _MANIFEST_PHASE_ROLLBACK_STARTED: (
                "backup_name", "current_name", "before_fingerprint",
                "candidate_fingerprint", "after_fingerprint", "receipt_hash",
                "event_hash", "mode",
            ),
            _MANIFEST_PHASE_ROLLED_BACK: (
                "before_fingerprint", "candidate_fingerprint", "after_fingerprint",
                "receipt_hash", "event_hash", "mode",
            ),
            _MANIFEST_PHASE_FINALIZING: (
                "before_fingerprint", "candidate_fingerprint", "after_fingerprint",
                "receipt_hash", "mode",
            ),
        }
        missing = [name for name in required.get(phase, ()) if getattr(self, name) is None]
        if missing:
            raise PromotionError(
                f"promotion manifest {phase} is missing required fields: {', '.join(missing)}"
            )
        if phase in {
            _MANIFEST_PHASE_PREPARED,
            _MANIFEST_PHASE_ACTIVE_MOVED,
            _MANIFEST_PHASE_SWAPPED,
        } and self.event_hash is not None:
            raise PromotionError("promotion manifest has an early event hash")
        if self.stage_fingerprint is not None and self.stage_name is None:
            raise PromotionError("promotion manifest has a stage fingerprint without a stage")
        if phase in {
            _MANIFEST_PHASE_COMMITTED,
            _MANIFEST_PHASE_ROLLBACK_STARTED,
            _MANIFEST_PHASE_ROLLED_BACK,
        } and self.stage_name is not None:
            raise PromotionError("promotion manifest retains a staging path too late")
        if phase in {_MANIFEST_PHASE_COMMITTED, _MANIFEST_PHASE_FINALIZING} and self.current_name is not None:
            raise PromotionError("promotion manifest retains a current path too late")
        if phase == _MANIFEST_PHASE_ROLLED_BACK and self.backup_name is not None:
            raise PromotionError("rolled-back promotion must not retain a backup name")
        object.__setattr__(self, "manifest_hash", self._compute_hash())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": PROMOTION_MANIFEST_SCHEMA_VERSION,
            "root_digest": self.root_digest,
            "phase": self.phase,
            "operation_id": self.operation_id,
            "backup_name": self.backup_name,
            "stage_name": self.stage_name,
            "stage_fingerprint": self.stage_fingerprint,
            "current_name": self.current_name,
            "before_fingerprint": self.before_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "receipt_hash": self.receipt_hash,
            "event_hash": self.event_hash,
            "mode": self.mode,
            "updated_at": self.updated_at,
        }

    def _compute_hash(self) -> str:
        return _manifest_payload_hash(self._payload())

    def verify(self) -> bool:
        return bool(self.manifest_hash) and self.manifest_hash == self._compute_hash()

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    def with_phase(self, phase: str, **changes: Any) -> "PromotionManifest":
        values = self.to_dict()
        values.pop("schema_version", None)
        values.pop("manifest_hash", None)
        values.update(changes)
        values["phase"] = phase
        values["updated_at"] = _utc_now()
        return PromotionManifest(**values)

    @classmethod
    def idle(cls, root_digest: str) -> "PromotionManifest":
        return cls(root_digest=root_digest, phase=_MANIFEST_PHASE_IDLE)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PromotionManifest":
        if not isinstance(data, Mapping):
            raise PromotionError("promotion manifest must be an object")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != PROMOTION_MANIFEST_SCHEMA_VERSION:
            raise PromotionError("unsupported promotion manifest schema")
        supplied = _bounded_text(data.get("manifest_hash"), 128).strip().lower()
        payload = dict(data)
        payload.pop("schema_version", None)
        payload.pop("manifest_hash", None)
        try:
            restored = cls(**payload)
        except (TypeError, ValueError, PromotionError) as exc:
            raise PromotionError("promotion manifest is invalid") from exc
        hash_matches = supplied == restored.manifest_hash
        if not hash_matches and "stage_fingerprint" not in data:
            # Schema 1 manifests written before ``stage_fingerprint`` was
            # introduced hashed the same payload without that key.  Accept
            # only that exact legacy shape, then return the normalized object
            # with a current hash.  Recovery remains fail-closed when such a
            # record would require deleting an unbound staging directory.
            legacy_payload = dict(data)
            legacy_payload.pop("manifest_hash", None)
            hash_matches = supplied == _manifest_payload_hash(legacy_payload)
        if not supplied or not hash_matches or not restored.verify():
            raise PromotionError("promotion manifest hash mismatch")
        return restored


def _manifest_file_is_safe(path: Path) -> None:
    """Validate an existing manifest file without opening unsafe aliases."""

    try:
        if _is_link_like(path) or not path.is_file():
            raise PromotionError("promotion manifest must be a regular file")
        stat_result = path.stat()
    except OSError as exc:
        raise PromotionError(f"cannot inspect promotion manifest: {path}") from exc
    if getattr(stat_result, "st_nlink", 1) > 1:
        raise PromotionError("promotion manifest hard links are not supported")
    if stat_result.st_size > PROMOTION_MANIFEST_MAX_BYTES:
        raise PromotionError("promotion manifest is too large")


def _read_promotion_manifest(path: Path) -> PromotionManifest | None:
    if _is_link_like(path):
        _manifest_file_is_safe(path)
    if not _lexists(path):
        return None
    _manifest_file_is_safe(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PromotionError("promotion manifest JSON is invalid") from exc
    return PromotionManifest.from_dict(payload)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability barrier on the current host.

    POSIX exposes directory descriptors directly.  Windows does not let
    ``os.open`` portably open a directory, but its ``FlushFileBuffers`` API
    does accept a handle opened with ``FILE_FLAG_BACKUP_SEMANTICS``.  The
    latter is deliberately best-effort: a filesystem may still decline the
    request, in which case the atomic rename and flushed manifest remain the
    recovery source of truth.
    """

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            flush_buffers = kernel32.FlushFileBuffers
            flush_buffers.argtypes = [ctypes.c_void_p]
            flush_buffers.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            # GENERIC_READ, share read/write/delete, OPEN_EXISTING,
            # FILE_FLAG_BACKUP_SEMANTICS, INVALID_HANDLE_VALUE.
            handle = create_file(
                str(path),
                0x80000000,
                0x00000001 | 0x00000002 | 0x00000004,
                None,
                3,
                0x02000000,
                None,
            )
            invalid = ctypes.c_void_p(-1).value
            if handle and handle != invalid:
                try:
                    flush_buffers(handle)
                finally:
                    close_handle(handle)
        except Exception:
            pass
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_promotion_manifest(path: Path, manifest: PromotionManifest) -> None:
    """Atomically replace a manifest with a flushed, hash-checked payload."""

    if not (
        path.name.startswith(".brain-memory-controller-")
        and path.name.endswith(".manifest.json")
    ):
        raise PromotionError("promotion manifest path is not controller-owned")
    if _is_link_like(path) or _lexists(path):
        _manifest_file_is_safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".brain-memory-manifest-tmp-",
            suffix=".json",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(_canonical(manifest.to_dict()))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise PromotionError(f"cannot persist promotion manifest: {path}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _is_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", _bounded_text(value, 128).strip().lower()))


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _epoch_from_iso(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        result = parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise SandboxAttestationError("sandbox attestation timestamp is invalid") from exc
    if not math.isfinite(result):
        raise SandboxAttestationError("sandbox attestation timestamp is not finite")
    return result


def _normalise_capabilities(value: Iterable[Any] | str | None) -> tuple[str, ...]:
    """Normalize host capability labels without accepting a self-report flag."""

    if value is None:
        values: Iterable[Any] = REQUIRED_SANDBOX_CAPABILITIES
    elif isinstance(value, str):
        values = (item.strip() for item in value.split(","))
    else:
        values = value
    aliases = {
        "fs_scope": "filesystem_scope",
        "filesystem": "filesystem_scope",
        "network": "network_denied",
        "network_isolated": "network_denied",
        "process_tree": "process_tree_kill",
        "process_kill": "process_tree_kill",
        "resources": "resource_limits",
        "resource_limits_enforced": "resource_limits",
    }
    result: set[str] = set()
    try:
        for raw in values:
            text = _bounded_text(raw, 100).strip().lower()
            if not text:
                continue
            result.add(aliases.get(text, text))
    except TypeError as exc:
        raise SandboxAttestationError("sandbox capabilities must be iterable") from exc
    if not REQUIRED_SANDBOX_CAPABILITIES.issubset(result):
        missing = sorted(REQUIRED_SANDBOX_CAPABILITIES.difference(result))
        raise SandboxAttestationError(
            "sandbox attestation is missing required capabilities: " + ", ".join(missing)
        )
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class SandboxAttestation:
    """Opaque host proof that one receipt ran inside an enforced sandbox.

    The object carries no signing secret.  Only :class:`SandboxAttestor` can
    issue or validate its HMAC; constructing an object with
    ``isolated=True``-style fields is therefore insufficient for production
    promotion.
    """

    attestation_id: str
    issuer_id: str
    candidate_fingerprint: str
    receipt_hash: str
    mode: str
    harness_version: str
    fixture_hash: str
    evaluator_hash: str
    capabilities: tuple[str, ...]
    issued_at: str
    expires_at: str
    nonce: str
    signature: str

    def __post_init__(self) -> None:
        for name, limit in (
            ("attestation_id", 160),
            ("issuer_id", 160),
            ("candidate_fingerprint", 128),
            ("receipt_hash", 128),
            ("mode", 40),
            ("harness_version", 80),
            ("fixture_hash", 128),
            ("evaluator_hash", 128),
            ("issued_at", 100),
            ("expires_at", 100),
            ("nonce", 160),
            ("signature", 128),
        ):
            object.__setattr__(self, name, _bounded_text(getattr(self, name), limit).strip())
        object.__setattr__(self, "mode", PromotionMode.parse(self.mode).value)
        object.__setattr__(self, "capabilities", _normalise_capabilities(self.capabilities))
        if not self.attestation_id or not self.issuer_id or not self.nonce:
            raise SandboxAttestationError("sandbox attestation identifiers are required")
        if not _is_sha256(self.candidate_fingerprint) or not _is_sha256(self.receipt_hash):
            raise SandboxAttestationError("sandbox attestation hashes are invalid")
        if not self.harness_version or not _is_sha256(self.fixture_hash) or not _is_sha256(self.evaluator_hash):
            raise SandboxAttestationError("sandbox attestation evaluator contract is invalid")
        issued = _epoch_from_iso(self.issued_at)
        expires = _epoch_from_iso(self.expires_at)
        if expires <= issued:
            raise SandboxAttestationError("sandbox attestation expiry must follow issuance")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signature.lower()):
            raise SandboxAttestationError("sandbox attestation signature is invalid")
        object.__setattr__(self, "signature", self.signature.lower())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "attestation_id": self.attestation_id,
            "issuer_id": self.issuer_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "receipt_hash": self.receipt_hash,
            "mode": self.mode,
            "harness_version": self.harness_version,
            "fixture_hash": self.fixture_hash,
            "evaluator_hash": self.evaluator_hash,
            "capabilities": list(self.capabilities),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    def signing_payload(self) -> bytes:
        return _canonical(self._payload()).encode("utf-8")

    @property
    def attestation_hash(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    @property
    def hash(self) -> str:
        return self.attestation_hash

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SandboxAttestation":
        if not isinstance(data, Mapping):
            raise SandboxAttestationError("sandbox attestation must be an object")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != ATTESTATION_SCHEMA_VERSION:
            raise SandboxAttestationError("unsupported sandbox attestation schema")
        payload = dict(data)
        payload.pop("schema_version", None)
        raw_caps = payload.get("capabilities", ())
        if not isinstance(raw_caps, (list, tuple, set)):
            raise SandboxAttestationError("sandbox attestation capabilities must be a list")
        payload["capabilities"] = tuple(raw_caps)
        try:
            return cls(**payload)
        except SandboxAttestationError:
            raise
        except (TypeError, ValueError) as exc:
            raise SandboxAttestationError("invalid sandbox attestation payload") from exc

    def verify_signature(self, secret: bytes) -> bool:
        if not isinstance(secret, (bytes, bytearray)):
            return False
        expected = hmac.new(bytes(secret), self.signing_payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def is_expired(self, *, now: float | None = None, skew_sec: float = 0.0) -> bool:
        current = time.time() if now is None else float(now)
        return current > _epoch_from_iso(self.expires_at) + float(skew_sec)

    def verify(self, secret: bytes | None = None, *, now: float | None = None) -> bool:
        """Verify only when the caller supplies the host-held secret."""

        if secret is None:
            return False
        try:
            return self.verify_signature(secret) and not self.is_expired(now=now)
        except (SandboxAttestationError, TypeError, ValueError):
            return False


class SandboxAttestor:
    """Host-held HMAC issuer and one-time verifier for sandbox proofs.

    The generated secret is process-local and is never written to a file or
    included in a receipt.  A production host should keep this object in the
    trusted orchestration process, separate from candidate code.
    """

    def __init__(
        self,
        *,
        issuer_id: str | None = None,
        secret: bytes | bytearray | None = None,
        clock: Any = None,
        max_ttl_sec: float = SANDBOX_ATTESTATION_MAX_TTL_SEC,
    ) -> None:
        if secret is None:
            secret = secrets.token_bytes(32)
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise SandboxAttestationError("sandbox attestor secret must be at least 16 bytes")
        self._secret = bytes(secret)
        self.issuer_id = _bounded_text(issuer_id, 160).strip() or f"host-{uuid.uuid4().hex}"
        self._clock = clock or time.time
        try:
            self.max_ttl_sec = float(max_ttl_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SandboxAttestationError("max_ttl_sec must be finite") from exc
        if not math.isfinite(self.max_ttl_sec) or self.max_ttl_sec <= 0:
            raise SandboxAttestationError("max_ttl_sec must be positive and finite")
        self._used_ids: set[str] = set()
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        candidate_fingerprint: str,
        receipt_hash: str,
        mode: PromotionMode | EvaluationMode | str,
        harness_version: str,
        fixture_hash: str,
        evaluator_hash: str,
        capabilities: Iterable[Any] | str | None = None,
        enforced: bool = True,
        ttl_sec: float | None = None,
        evaluation_receipt_hash: str | None = None,
    ) -> SandboxAttestation:
        """Issue a proof after the host executor has enforced its boundary."""

        if enforced is not True:
            raise SandboxAttestationError("only an enforced host sandbox may issue attestation")
        if evaluation_receipt_hash is not None:
            if receipt_hash != evaluation_receipt_hash:
                raise SandboxAttestationError("receipt_hash aliases disagree")
            receipt_hash = evaluation_receipt_hash
        if not _is_sha256(candidate_fingerprint) or not _is_sha256(receipt_hash):
            raise SandboxAttestationError("attestation candidate/receipt hash is invalid")
        harness_version = _bounded_text(harness_version, 80).strip()
        fixture_hash = _bounded_text(fixture_hash, 128).strip().lower()
        evaluator_hash = _bounded_text(evaluator_hash, 128).strip().lower()
        if not harness_version or not _is_sha256(fixture_hash) or not _is_sha256(evaluator_hash):
            raise SandboxAttestationError("attestation evaluator contract is invalid")
        caps = _normalise_capabilities(capabilities)
        try:
            ttl = self.max_ttl_sec if ttl_sec is None else float(ttl_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SandboxAttestationError("ttl_sec must be finite") from exc
        if not math.isfinite(ttl) or ttl <= 0 or ttl > self.max_ttl_sec:
            raise SandboxAttestationError(
                f"ttl_sec must be in (0, {self.max_ttl_sec:g}]"
            )
        now = float(self._clock())
        if not math.isfinite(now):
            raise SandboxAttestationError("host clock is not finite")
        issued_at = _iso_from_epoch(now)
        expires_at = _iso_from_epoch(now + ttl)
        payload = {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "attestation_id": f"att-{uuid.uuid4().hex}",
            "issuer_id": self.issuer_id,
            "candidate_fingerprint": _bounded_text(candidate_fingerprint, 128).strip().lower(),
            "receipt_hash": _bounded_text(receipt_hash, 128).strip().lower(),
            "mode": PromotionMode.parse(mode).value,
            "harness_version": harness_version,
            "fixture_hash": fixture_hash,
            "evaluator_hash": evaluator_hash,
            "capabilities": list(caps),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "nonce": secrets.token_urlsafe(24),
        }
        signature = hmac.new(self._secret, _canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()
        payload["signature"] = signature
        return SandboxAttestation.from_dict(payload)

    def issue_for_receipt(
        self,
        receipt: EvaluationReceipt,
        *,
        capabilities: Iterable[Any] | str | None = None,
        enforced: bool = True,
        ttl_sec: float | None = None,
    ) -> SandboxAttestation:
        if type(receipt) is not EvaluationReceipt or not receipt.verify():
            raise SandboxAttestationError("only a verified fixed EvaluationReceipt can be attested")
        return self.issue(
            candidate_fingerprint=receipt.candidate_fingerprint,
            receipt_hash=receipt.receipt_hash,
            mode=receipt.mode,
            harness_version=receipt.harness_version,
            fixture_hash=receipt.fixture_hash,
            evaluator_hash=receipt.evaluator_hash,
            capabilities=capabilities,
            enforced=enforced,
            ttl_sec=ttl_sec,
        )

    def validate(
        self,
        value: SandboxAttestation | Mapping[str, Any],
        *,
        candidate_fingerprint: str | None = None,
        receipt_hash: str | None = None,
        mode: PromotionMode | EvaluationMode | str | None = None,
        harness_version: str | None = None,
        fixture_hash: str | None = None,
        evaluator_hash: str | None = None,
        required_capabilities: Iterable[Any] | str | None = None,
        consume: bool = False,
        now: float | None = None,
    ) -> SandboxAttestation:
        if isinstance(value, SandboxAttestation):
            attestation = value
        elif isinstance(value, Mapping):
            attestation = SandboxAttestation.from_dict(value)
        else:
            raise SandboxAttestationError("sandbox attestation must be host-issued")
        with self._lock:
            if attestation.issuer_id != self.issuer_id:
                raise SandboxAttestationError("sandbox attestation issuer is not trusted")
            if attestation.attestation_id in self._used_ids:
                raise SandboxAttestationError("sandbox attestation has already been consumed")
            if not attestation.verify_signature(self._secret):
                raise SandboxAttestationError("sandbox attestation signature mismatch")
            current = float(self._clock()) if now is None else float(now)
            if not math.isfinite(current) or attestation.is_expired(now=current, skew_sec=5.0):
                raise SandboxAttestationError("sandbox attestation is expired")
            issued = _epoch_from_iso(attestation.issued_at)
            if current + 5.0 < issued:
                raise SandboxAttestationError("sandbox attestation is not yet valid")
            checks = (
                ("candidate fingerprint", candidate_fingerprint, attestation.candidate_fingerprint),
                ("receipt hash", receipt_hash, attestation.receipt_hash),
                ("harness version", harness_version, attestation.harness_version),
                ("fixture hash", fixture_hash, attestation.fixture_hash),
                ("evaluator hash", evaluator_hash, attestation.evaluator_hash),
            )
            for label, expected, actual in checks:
                if expected is not None and str(expected).strip().lower() != str(actual).strip().lower():
                    raise SandboxAttestationError(f"sandbox attestation {label} mismatch")
            if mode is not None and PromotionMode.parse(mode).value != attestation.mode:
                raise SandboxAttestationError("sandbox attestation mode mismatch")
            required = _normalise_capabilities(required_capabilities) if required_capabilities is not None else REQUIRED_SANDBOX_CAPABILITIES
            if not set(required).issubset(set(attestation.capabilities)):
                raise SandboxAttestationError("sandbox attestation lacks required host capabilities")
            if consume:
                self._used_ids.add(attestation.attestation_id)
            return attestation

    def verify(self, value: SandboxAttestation | Mapping[str, Any], **kwargs: Any) -> bool:
        try:
            self.validate(value, **kwargs)
            return True
        except (SandboxAttestationError, TypeError, ValueError):
            return False

    @property
    def consumed_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._used_ids)


# Explicit aliases for host orchestration adapters.
HostSandboxAttestation = SandboxAttestation
HostSandboxAttestor = SandboxAttestor
SandboxExecutorAttestation = SandboxAttestation


class CandidateSandbox:
    """A disposable copy of a candidate source tree.

    ``CandidateSandbox`` never exposes the source tree to a child process.  A
    caller may mutate ``sandbox.path`` while evaluating; ``verify()`` checks
    that the original source stayed unchanged, while ``fingerprint`` remains
    the source fingerprint captured at materialization time.
    """

    def __init__(
        self,
        source: str | os.PathLike[str] | CandidateRevision,
        *,
        protected_paths: Sequence[str | os.PathLike[str]] = (),
        temp_parent: str | os.PathLike[str] | None = None,
        resource_budget: ResourceBudget | None = None,
        budget: ResourceBudget | None = None,
    ) -> None:
        if isinstance(source, CandidateRevision):
            source_path = source.source_path
            expected = source.fingerprint
        else:
            source_path = source
            expected = None
        self.source = _resolve_local(source_path, strict=True)
        if _is_link_like(self.source) or not self.source.is_dir():
            raise CandidateIsolationError(f"candidate must be a real directory: {self.source}")
        if resource_budget is not None and budget is not None and resource_budget != budget:
            raise CandidateIsolationError("resource_budget and budget disagree")
        selected_budget = resource_budget if resource_budget is not None else budget
        if selected_budget is not None and not isinstance(selected_budget, ResourceBudget):
            raise CandidateIsolationError("resource_budget must be a ResourceBudget")
        self.resource_budget = selected_budget or ResourceBudget()
        if expected is not None and expected != directory_fingerprint(
            self.source,
            budget=self.resource_budget,
        ):
            raise CandidateIsolationError("candidate descriptor fingerprint is stale")
        self.fingerprint = directory_fingerprint(self.source, budget=self.resource_budget)
        self.protected_paths = tuple(_normalise_rel(item) for item in protected_paths)
        self._temp_parent = (
            _resolve_local(temp_parent, strict=True) if temp_parent is not None else None
        )
        if self._temp_parent is not None and not self._temp_parent.is_dir():
            raise CandidateIsolationError("sandbox temp_parent must be a directory")
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._path: Path | None = None
        self._closed = False
        self._source_fingerprint_after: str | None = None

    @property
    def path(self) -> Path:
        if self._path is None or self._closed:
            raise CandidateIsolationError("candidate sandbox is not active")
        return self._path

    @property
    def root(self) -> Path:
        return self.path

    @property
    def workspace(self) -> Path:
        return self.path

    def __enter__(self) -> "CandidateSandbox":
        if self._path is not None and not self._closed:
            return self
        if directory_fingerprint(self.source, budget=self.resource_budget) != self.fingerprint:
            raise CandidateIsolationError("candidate changed before sandbox materialization")
        kwargs: dict[str, Any] = {"prefix": "brain-memory-candidate-"}
        if self._temp_parent is not None:
            kwargs["dir"] = str(self._temp_parent)
        self._temporary = tempfile.TemporaryDirectory(**kwargs)
        self._path = Path(self._temporary.name) / "candidate"
        _copy_tree(
            self.source,
            self._path,
            exclude=self.protected_paths,
            budget=self.resource_budget,
        )
        # A protected path is intentionally omitted from a candidate sandbox;
        # the controller restores it from the trusted active tree when staging.
        self._closed = False
        return self

    def verify_source_unchanged(self) -> bool:
        try:
            self._source_fingerprint_after = directory_fingerprint(
                self.source,
                budget=self.resource_budget,
            )
        except (OSError, EvaluationHarnessError, PromotionError):
            self._source_fingerprint_after = None
        return self._source_fingerprint_after == self.fingerprint

    def verify(self) -> bool:
        """Verify source immutability and sandbox liveness.

        Changes inside the disposable copy are allowed by design.
        """

        return bool(self._path is not None and not self._closed and self.verify_source_unchanged())

    def evaluate(
        self,
        harness: EvaluationHarness,
        baseline: BaselineRevision | Mapping[str, Any] | str | os.PathLike[str],
        *,
        mode: PromotionMode | EvaluationMode | str = EVOLUTION,
        **kwargs: Any,
    ) -> EvaluationReceipt:
        """Evaluate the original candidate through a fixed harness."""

        if not isinstance(harness, EvaluationHarness):
            raise PromotionError("CandidateSandbox.evaluate requires EvaluationHarness")
        return harness.evaluate(self.source, baseline, mode=PromotionMode.parse(mode).value, **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._temporary is not None:
            self._temporary.cleanup()
        self._temporary = None
        self._path = None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # Capture source integrity before disposing the copy.  Do not mask a
        # body exception; callers can inspect ``verify_source_unchanged``
        # through the retained fingerprint or controller receipt.
        self.verify_source_unchanged()
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """Immutable result of a promotion or rollback attempt."""

    accepted: bool
    action: str
    mode: str
    candidate_revision_id: str | None = None
    candidate_fingerprint: str | None = None
    evaluation_receipt_hash: str | None = None
    active_before_fingerprint: str | None = None
    active_after_fingerprint: str | None = None
    rollback_available: bool = False
    record_hash: str = ""
    reason: str = ""
    sandbox_attestation_id: str | None = None
    sandbox_attestation_hash: str | None = None
    attestation_verified: bool = False

    @property
    def success(self) -> bool:
        return self.accepted

    @property
    def promoted(self) -> bool:
        return self.action == "promoted" and self.accepted

    @property
    def receipt_hash(self) -> str:
        return self.record_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "accepted": self.accepted,
            "action": self.action,
            "mode": self.mode,
            "candidate_revision_id": self.candidate_revision_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "evaluation_receipt_hash": self.evaluation_receipt_hash,
            "active_before_fingerprint": self.active_before_fingerprint,
            "active_after_fingerprint": self.active_after_fingerprint,
            "rollback_available": self.rollback_available,
            "record_hash": self.record_hash,
            "reason": self.reason,
            "sandbox_attestation_id": self.sandbox_attestation_id,
            "sandbox_attestation_hash": self.sandbox_attestation_hash,
            "attestation_verified": self.attestation_verified,
        }


@dataclass(frozen=True, slots=True)
class PromotionEvent:
    """One append-only, hash-addressed ledger record."""

    sequence: int
    event_id: str
    timestamp: str
    action: str
    mode: str
    candidate_revision_id: str | None
    candidate_fingerprint: str | None
    evaluation_receipt_hash: str | None
    active_before_fingerprint: str | None
    active_after_fingerprint: str | None
    authorized: bool
    reason: str = ""
    previous_hash: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    event_hash: str = ""

    def __post_init__(self) -> None:
        sequence = int(self.sequence)
        if sequence < 1:
            raise LedgerError("ledger sequence must be positive")
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "event_id", _bounded_text(self.event_id, 160) or f"event-{uuid.uuid4().hex}")
        object.__setattr__(self, "timestamp", _bounded_text(self.timestamp, 100) or _utc_now())
        object.__setattr__(self, "action", _bounded_text(self.action, 80).strip())
        object.__setattr__(self, "mode", PromotionMode.parse(self.mode).value)
        for name in (
            "candidate_revision_id",
            "candidate_fingerprint",
            "evaluation_receipt_hash",
            "active_before_fingerprint",
            "active_after_fingerprint",
            "previous_hash",
        ):
            value = _bounded_text(getattr(self, name), 160).strip() or None
            object.__setattr__(self, name, value)
        object.__setattr__(self, "authorized", self.authorized is True)
        object.__setattr__(self, "reason", _bounded_text(self.reason, 1000))
        if _contains_sensitive_metadata(self.metadata):
            raise LedgerError("promotion metadata contains a credential-shaped field")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "event_hash", self._compute_hash())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "mode": self.mode,
            "candidate_revision_id": self.candidate_revision_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "evaluation_receipt_hash": self.evaluation_receipt_hash,
            "active_before_fingerprint": self.active_before_fingerprint,
            "active_after_fingerprint": self.active_after_fingerprint,
            "authorized": self.authorized,
            "reason": self.reason,
            "previous_hash": self.previous_hash or "",
            "metadata": _thaw(self.metadata),
        }

    def _compute_hash(self) -> str:
        return hashlib.sha256(_canonical(self._payload()).encode("utf-8")).hexdigest()

    def verify(self, *, expected_previous: str | None = None, expected_sequence: int | None = None) -> bool:
        if expected_previous is not None and (self.previous_hash or "") != expected_previous:
            return False
        if expected_sequence is not None and self.sequence != expected_sequence:
            return False
        return bool(re.fullmatch(r"[0-9a-f]{64}", self.event_hash or "")) and self.event_hash == self._compute_hash()

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["event_hash"] = self.event_hash
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, verify_hash: bool = True) -> "PromotionEvent":
        if not isinstance(data, Mapping):
            raise LedgerError("ledger event must be an object")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != PROMOTION_SCHEMA_VERSION:
            raise LedgerError("unsupported ledger event schema")
        supplied = _bounded_text(data.get("event_hash"), 128).strip().lower()
        payload = dict(data)
        payload.pop("schema_version", None)
        payload.pop("event_hash", None)
        try:
            event = cls(**payload)
        except (TypeError, ValueError, PromotionError) as exc:
            raise LedgerError("invalid ledger event") from exc
        if verify_hash and supplied != event.event_hash:
            raise LedgerError("ledger event hash mismatch")
        if not verify_hash:
            object.__setattr__(event, "event_hash", supplied)
        return event


class PromotionLedger:
    """Thread-safe append-only event ledger, optionally persisted as JSONL."""

    def __init__(
        self,
        events: Iterable[PromotionEvent | Mapping[str, Any]] = (),
        *,
        path: str | os.PathLike[str] | None = None,
        verify: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._path = _resolve_local(path, strict=False) if path is not None else None
        if self._path is not None and _is_sensitive_name(self._path.name):
            raise LedgerError("ledger path looks like a credential/private file")
        self._events: list[PromotionEvent] = []
        for raw in events:
            event = raw if isinstance(raw, PromotionEvent) else PromotionEvent.from_dict(raw)
            self._events.append(event)
        if self._path is not None:
            if self._path.exists():
                if not self._path.is_file():
                    raise LedgerError("ledger path must be a file")
                _assert_single_link_file(self._path)
                # A second process may be appending while this object is being
                # opened.  Read the immutable file under the same OS lock used
                # by append/verify so construction never observes a partial
                # line.
                with _promotion_ledger_file_lock(self._path):
                    loaded = self._read_file(self._path, verify_hash=verify)
                if self._events and [item.to_dict() for item in loaded] != [item.to_dict() for item in self._events]:
                    raise LedgerError("ledger events disagree with persisted file")
                self._events = loaded
            elif self._events:
                # Supplied in-memory history must not be silently discarded
                # when the backing file vanished before the first append.
                raise LedgerError("promotion ledger file is missing")
        if verify and not self.verify():
            raise LedgerError("ledger hash chain is invalid")

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def events(self) -> tuple[PromotionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def records(self) -> tuple[PromotionEvent, ...]:
        return self.events

    @property
    def last_hash(self) -> str:
        return self._events[-1].event_hash if self._events else ""

    def __len__(self) -> int:
        return len(self._events)

    def _read_file(self, path: Path, *, verify_hash: bool) -> list[PromotionEvent]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LedgerError(f"cannot read promotion ledger: {path}") from exc
        loaded: list[PromotionEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerError("ledger contains invalid JSON") from exc
            loaded.append(PromotionEvent.from_dict(data, verify_hash=verify_hash))
        if verify_hash:
            previous = ""
            for index, event in enumerate(loaded, start=1):
                if not event.verify(expected_previous=previous, expected_sequence=index):
                    raise LedgerError("ledger hash chain is invalid")
                previous = event.event_hash
        return loaded

    def _assert_file_matches_memory(self) -> None:
        if self._path is None:
            return
        if not self._path.exists():
            if self._events:
                raise LedgerError("promotion ledger file disappeared")
            return
        _assert_single_link_file(self._path)
        loaded = self._read_file(self._path, verify_hash=True)
        if [event.to_dict() for event in loaded] != [event.to_dict() for event in self._events]:
            raise LedgerError("ledger was modified outside append-only API")

    def append(
        self,
        *,
        action: str,
        mode: PromotionMode | EvaluationMode | str = EVOLUTION,
        candidate_revision_id: str | None = None,
        candidate_fingerprint: str | None = None,
        evaluation_receipt_hash: str | None = None,
        active_before_fingerprint: str | None = None,
        active_after_fingerprint: str | None = None,
        authorized: bool = False,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> PromotionEvent:
        with self._lock:
            if self._path is None:
                self._assert_file_matches_memory()
                event = PromotionEvent(
                    sequence=len(self._events) + 1,
                    event_id=f"event-{uuid.uuid4().hex}",
                    timestamp=_utc_now(),
                    action=action,
                    mode=PromotionMode.parse(mode).value,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    evaluation_receipt_hash=evaluation_receipt_hash,
                    active_before_fingerprint=active_before_fingerprint,
                    active_after_fingerprint=active_after_fingerprint,
                    authorized=authorized,
                    reason=reason,
                    previous_hash=self.last_hash,
                    metadata=metadata or {},
                )
                self._events.append(event)
                return event

            # The file lock spans the disk consistency check, event creation,
            # append, fsync, and in-memory commit.  A stale second process
            # therefore fails closed instead of producing duplicate sequence
            # numbers or a broken previous-hash chain.
            with _promotion_ledger_file_lock(self._path):
                if self._path.exists():
                    _assert_single_link_file(self._path)
                self._assert_file_matches_memory()
                event = PromotionEvent(
                    sequence=len(self._events) + 1,
                    event_id=f"event-{uuid.uuid4().hex}",
                    timestamp=_utc_now(),
                    action=action,
                    mode=PromotionMode.parse(mode).value,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    evaluation_receipt_hash=evaluation_receipt_hash,
                    active_before_fingerprint=active_before_fingerprint,
                    active_after_fingerprint=active_after_fingerprint,
                    authorized=authorized,
                    reason=reason,
                    previous_hash=self.last_hash,
                    metadata=metadata or {},
                )
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                        stream.write(_canonical(event.to_dict()) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as exc:
                    raise LedgerError(f"cannot append promotion ledger: {self._path}") from exc
                self._events.append(event)
                return event

    # Explicit aliases are useful to persistence adapters and make it harder
    # for a caller to mistake this for an update/delete-capable store.
    append_event = append
    append_receipt = append

    def verify(self) -> bool:
        with self._lock:
            if self._path is None:
                previous = ""
                for index, event in enumerate(self._events, start=1):
                    if not event.verify(expected_previous=previous, expected_sequence=index):
                        return False
                    previous = event.event_hash
                return True
            with _promotion_ledger_file_lock(self._path):
                if self._path.exists():
                    try:
                        _assert_single_link_file(self._path)
                    except LedgerError:
                        return False
                previous = ""
                for index, event in enumerate(self._events, start=1):
                    if not event.verify(expected_previous=previous, expected_sequence=index):
                        return False
                    previous = event.event_hash
                if self._path.exists():
                    try:
                        loaded = self._read_file(self._path, verify_hash=True)
                    except LedgerError:
                        return False
                    if [event.to_dict() for event in loaded] != [event.to_dict() for event in self._events]:
                        return False
                elif self._events:
                    return False
                return True

    def reload(self) -> "PromotionLedger":
        """Refresh a file-backed ledger while holding its append lock.

        Controllers can be constructed in more than one process before either
        process records a rejection or promotion.  Reloading under the same
        sidecar lock prevents a stale in-memory sequence/head from turning a
        safe concurrent decision into an avoidable ``LedgerError``.  The
        persisted hash chain remains the source of truth; malformed or
        hard-linked files still fail closed.
        """

        with self._lock:
            if self._path is None:
                return self
            with _promotion_ledger_file_lock(self._path):
                if self._path.exists():
                    if not self._path.is_file():
                        raise LedgerError("ledger path must be a file")
                    _assert_single_link_file(self._path)
                    self._events = self._read_file(self._path, verify_hash=True)
                elif self._events:
                    raise LedgerError("promotion ledger file disappeared")
                else:
                    self._events = []
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]

    def snapshot(self) -> dict[str, Any]:
        return {"schema_version": PROMOTION_SCHEMA_VERSION, "events": self.to_list()}

    @classmethod
    def from_snapshot(cls, payload: Mapping[str, Any], *, verify: bool = True) -> "PromotionLedger":
        if not isinstance(payload, Mapping):
            raise LedgerError("ledger snapshot must be an object")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != PROMOTION_SCHEMA_VERSION:
            raise LedgerError("unsupported ledger snapshot schema")
        raw_events = payload.get("events", ())
        if not isinstance(raw_events, (list, tuple)):
            raise LedgerError("ledger snapshot events must be a list")
        return cls(raw_events, verify=verify)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str], *, verify: bool = True) -> "PromotionLedger":
        return cls(path=path, verify=verify)


# The hash-chained event is also the promotion receipt exposed to callers.
PromotionReceipt = PromotionEvent
HashReceipt = PromotionEvent


_REQUIRED_COMMON_GATES = {
    HardGate.STARTUP.value,
    HardGate.INTEGRITY.value,
    HardGate.RESOURCE_BUDGET.value,
    HardGate.PERMISSION_BOUNDARY.value,
    HardGate.INDEPENDENT_JUDGE.value,
    HardGate.BASELINE_CONTRACT.value,
    HardGate.VERIFICATION.value,
    HardGate.ROLLBACK.value,
}


class PromotionController:
    """Validate evaluator evidence and atomically promote a local candidate.

    The default ``production`` profile requires a
    :class:`SandboxAttestation` issued by a trusted :class:`SandboxAttestor`;
    the attestation is checked against the exact receipt immediately before
    the active-tree swap and consumed once.  ``profile="legacy"`` is an
    explicit compatibility/test escape hatch and carries no host-isolation
    claim.
    """

    # These defaults protect the evaluator/lifecycle seams when the active
    # root is a project checkout.  Callers should add project-specific paths.
    DEFAULT_PROTECTED_PATHS = (
        ".git",
        ".gitignore",
        ".env",
        ".env.example",
        "AGENTS.md",
        "CONTEXT.md",
        "README.md",
        "version.py",
        "config.py",
        "start.bat",
        "requirements.txt",
        "requirements.lock",
        "requirements-dev.txt",
        "requirements-dev.lock",
        "RELEASE_v0.1.md",
        "RELEASE_v10.0.md",
        # Runtime enforcement is broader than the promotion primitive itself.
        # Keep authorization, causal correlation, deterministic outcome
        # verification and source/network boundaries outside the candidate's
        # write domain as well.  Hosts may add paths, but cannot remove these
        # defaults by passing an empty/custom ``protected_files`` value.
        "brain/autonomy.py",
        "brain/boundary.py",
        "brain/cognitive_dispatch.py",
        "brain/core.py",
        "brain/evaluation_harness.py",
        "brain/evolution.py",
        "brain/intent.py",
        "brain/learning_feedback.py",
        "brain/life_kernel.py",
        "brain/brain_stem.py",
        "brain/brain_state.py",
        "brain/core_purpose.py",
        "brain/task_execution.py",
        "brain/task_scheduler.py",
        "brain/schemas.py",
        "brain/__init__.py",
        # Constitutional/runtime policy and its durable audit adapters are
        # outside the MutableOrganism's write domain.  Missing paths are still
        # protected from being introduced by a candidate, so a project host
        # can safely use this default before every file exists.
        "brain/succession.py",
        "brain/succession_runtime.py",
        "brain/homeostasis.py",
        "brain/motivation.py",
        "storage/database.py",
        "storage/__init__.py",
        "agent_bridge.py",
        "agent",
        "api",
        "services/llm_client.py",
        "services/llm_prompts.py",
        "services/source_adapter.py",
        "services/__init__.py",
        "docs/adr",
        "docs/research",
        "docs/verification",
        "fixtures",
        "tests",
        # Test/dependency discovery controls are evaluator-owned even when a
        # project does not currently contain them.  Protecting an absent path
        # prevents a candidate from introducing its own collection policy or
        # startup hook and then presenting that as independent evidence.
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "sitecustomize.py",
        "usercustomize.py",
        # The repository keeps its executable regression fixtures at the
        # root.  Keep the current set explicit because protected paths are
        # intentionally literal and deterministic (no wildcard expansion).
        "test_evaluation_harness.py",
        "test_evolution.py",
        "test_evolution_protected_surface.py",
        "test_homeostasis.py",
        "test_life_kernel.py",
        "test_living_world_soak.py",
        "test_long_run_invariants.py",
        "test_motivation.py",
        "test_motivation_replay.py",
        "test_self_maintenance_integration.py",
        "test_succession.py",
        "test_succession_runtime.py",
        "test_succession_stem.py",
        "test_succession_store.py",
        "test_api_readonly.py",
        "test_autonomy.py",
        "test_brain_stem_iteration.py",
        "test_consciousness_chain.py",
        "test_learning_feedback.py",
        "test_source_adapter.py",
        "test_stability_baseline.py",
        "test_task_execution.py",
        "test_task_scheduler.py",
        "test_v5_integration.py",
        "test_v6_integration.py",
        "test_v7_integration.py",
        "test_v8_integration.py",
        "test_v9_integration.py",
        "test_v10_integration.py",
        "test_v13_runtime_smoke.py",
    )

    def __init__(
        self,
        active_root: str | os.PathLike[str] | None = None,
        *,
        active_path: str | os.PathLike[str] | None = None,
        root: str | os.PathLike[str] | None = None,
        harness: EvaluationHarness | None = None,
        evaluation_harness: EvaluationHarness | None = None,
        protected_files: Sequence[str | os.PathLike[str]] | None = None,
        protected_paths: Sequence[str | os.PathLike[str]] | None = None,
        ledger: PromotionLedger | None = None,
        promotion_ledger: PromotionLedger | None = None,
        ledger_path: str | os.PathLike[str] | None = None,
        host_authorized: bool = False,
        profile: str = "production",
        require_sandbox_attestation: bool | None = None,
        sandbox_attestor: SandboxAttestor | None = None,
        attestor: SandboxAttestor | None = None,
        harness_version: str | None = None,
        fixture_hash: str | None = None,
        evaluator_hash: str | None = None,
        resource_budget: ResourceBudget | None = None,
        budget: ResourceBudget | None = None,
        persistence_path: str | os.PathLike[str] | None = None,
    ) -> None:
        roots = [item for item in (active_root, active_path, root) if item is not None]
        if not roots:
            raise PromotionError("active_root is required")
        # The active directory may be temporarily absent while a previous
        # controller is between the two atomic renames.  Resolve the name
        # without requiring existence, establish the root-level lock and
        # reconcile the manifest below, then enforce the real-directory
        # invariant before taking any fingerprint.
        resolved_roots = {_resolve_local(item, strict=False) for item in roots}
        if len(resolved_roots) != 1:
            raise PromotionError("active_root/active_path/root disagree")
        self.active_root = next(iter(resolved_roots))
        self._root_digest = _root_identity_digest(self.active_root)
        self._controller_lock_path = (
            self.active_root.parent / f".brain-memory-controller-{self._root_digest}.lock"
        )
        self._manifest_path = _manifest_path_for(self.active_root)
        self._backup_path: Path | None = None
        self._planned_backup_path: Path | None = None
        self._active_move_manifest: PromotionManifest | None = None
        self._retired_paths: list[Path] = []
        # Fingerprints bind in-process retired names to the tree that this
        # controller moved there.  ``close()`` must not recursively delete a
        # same-named replacement created by another process.
        self._retired_fingerprints: dict[Path, str] = {}
        self._lock = threading.RLock()
        self._last_outcome: PromotionOutcome | None = None
        if harness is not None and evaluation_harness is not None and harness is not evaluation_harness:
            raise PromotionError("harness and evaluation_harness disagree")
        self.harness = harness or evaluation_harness
        supplied_contract = (harness_version, fixture_hash, evaluator_hash)
        if self.harness is not None:
            expected_contract = (
                self.harness.harness_version,
                self.harness.fixture_hash,
                self.harness.evaluator_hash,
            )
            if any(value is not None and str(value) != str(expected) for value, expected in zip(supplied_contract, expected_contract)):
                raise PromotionError("explicit evaluator contract disagrees with harness")
            supplied_contract = expected_contract
        self.harness_version, self.fixture_hash, self.evaluator_hash = supplied_contract
        if resource_budget is not None and budget is not None and resource_budget != budget:
            raise PromotionError("resource_budget and budget disagree")
        selected_budget = resource_budget if resource_budget is not None else budget
        if selected_budget is None and self.harness is not None:
            selected_budget = self.harness.resource_budget
        # A low-level controller can be used without an evaluator (for
        # explicit legacy recovery), so still impose a finite local ceiling
        # instead of allowing an unbounded whole-tree copy.
        if selected_budget is not None and not isinstance(selected_budget, ResourceBudget):
            raise PromotionError("resource_budget must be a ResourceBudget")
        self.resource_budget = selected_budget or ResourceBudget()
        profile_text = _bounded_text(profile, 40).strip().lower() or "production"
        profile_aliases = {
            "prod": "production",
            "strict": "production",
            "host": "production",
            "test": "legacy",
            "dev": "legacy",
            "local": "legacy",
        }
        profile_text = profile_aliases.get(profile_text, profile_text)
        if profile_text not in {"production", "legacy"}:
            raise PromotionError("profile must be 'production' or explicit 'legacy'")
        self.profile = profile_text
        if profile_text == "production" and persistence_path is None:
            raise PromotionError(
                "production profile requires an explicit external persistence_path"
            )
        self.persistence_path: Path | None = None
        if persistence_path is not None:
            self.persistence_path = self._validate_persistence_location(persistence_path)
        if protected_files is not None and protected_paths is not None:
            if tuple(map(os.fspath, protected_files)) != tuple(map(os.fspath, protected_paths)):
                raise PromotionError("protected_files and protected_paths disagree")
        selected_extra = (
            protected_files
            if protected_files is not None
            else protected_paths
        )
        # The constitutional/runtime paths are a floor, not a replaceable
        # convenience list.  A host may add project-specific protected paths,
        # but an empty/custom list must not silently make the evaluator,
        # lifecycle kernel, authorization boundary, or product identity
        # writable by a candidate.
        selected_protected = list(self.DEFAULT_PROTECTED_PATHS)
        if selected_extra is not None:
            selected_protected.extend(selected_extra)
        self.protected_paths = tuple(
            dict.fromkeys(_normalise_rel(item) for item in selected_protected)
        )
        if ledger is not None and promotion_ledger is not None and ledger is not promotion_ledger:
            raise PromotionError("ledger and promotion_ledger disagree")
        provided_ledger = ledger if ledger is not None else promotion_ledger
        if ledger_path is not None:
            requested_path = _resolve_local(ledger_path, strict=False)
            self._assert_ledger_location(requested_path)
            persisted = PromotionLedger.from_path(requested_path)
            if provided_ledger is not None and provided_ledger.path != persisted.path:
                raise PromotionError("ledger and ledger_path disagree")
            selected_ledger = persisted
        else:
            # Do not use truthiness here: an empty PromotionLedger is
            # intentionally false because it implements ``__len__``.
            selected_ledger = provided_ledger if provided_ledger is not None else PromotionLedger()
        # The active tree is atomically renamed during promotion.  A
        # promotion ledger located inside it would be moved out from under
        # the controller mid-transaction, potentially losing the only audit
        # proof of the swap.  Require an external audit path.
        ledger_location = getattr(selected_ledger, "path", None)
        if ledger_location is not None and self._same_or_nested(
            Path(ledger_location), self.active_root
        ):
            raise PromotionError(
                "promotion ledger must be outside the atomically swapped active tree"
            )
        if ledger_location is not None:
            self._assert_ledger_location(Path(ledger_location))
        if profile_text == "production" and ledger_location is None:
            raise PromotionError(
                "production profile requires an external file-backed promotion ledger"
            )
        self.ledger = selected_ledger
        self.host_authorized = host_authorized is True
        if require_sandbox_attestation is not None and not isinstance(require_sandbox_attestation, bool):
            raise PromotionError("require_sandbox_attestation must be a boolean")
        if profile_text == "production" and require_sandbox_attestation is False:
            raise PromotionError("production profile cannot disable sandbox attestation")
        self.require_sandbox_attestation = (
            profile_text == "production"
            if require_sandbox_attestation is None
            else require_sandbox_attestation
        )
        if sandbox_attestor is not None and attestor is not None and sandbox_attestor is not attestor:
            raise PromotionError("sandbox_attestor and attestor disagree")
        selected_attestor = sandbox_attestor if sandbox_attestor is not None else attestor
        if selected_attestor is not None and not isinstance(selected_attestor, SandboxAttestor):
            raise PromotionError("sandbox_attestor must be a trusted SandboxAttestor")
        self.sandbox_attestor = selected_attestor
        # Reconcile any interrupted swap before inspecting protected paths or
        # computing the live fingerprint.  The root lock covers both the
        # manifest and the directory names, so another controller cannot race
        # this recovery decision.
        with _promotion_ledger_file_lock(self._controller_lock_path):
            initial_manifest = _read_promotion_manifest(self._manifest_path)
            self._assert_no_unreferenced_siblings(initial_manifest)
            self._assert_transaction_sensitive_free(initial_manifest)
            self._recover_promotion_manifest()
            recovered_manifest = _read_promotion_manifest(self._manifest_path)
            self._assert_no_unreferenced_siblings(recovered_manifest)
            self._assert_transaction_sensitive_free(recovered_manifest)
        if _is_link_like(self.active_root) or not self.active_root.is_dir():
            raise PromotionError("active_root must be a real local directory")
        try:
            externally_managed = _external_managed_entries(self.active_root)
        except CandidateIsolationError as exc:
            raise PromotionError("active_root cannot be inspected safely") from exc
        if externally_managed:
            raise PromotionError(
                "active_root must be a runtime tree outside VCS/dependency metadata: "
                + ", ".join(externally_managed)
            )
        try:
            mutable_state_active = _mutable_state_entries(self.active_root)
        except CandidateIsolationError as exc:
            raise PromotionError("active_root cannot be inspected safely") from exc
        if mutable_state_active:
            raise PromotionError(
                "active_root contains mutable database state; configure an external persistence path: "
                + ", ".join(mutable_state_active)
            )
        # A whole-tree swap cannot preserve a private file that the evaluator
        # deliberately refuses to read.  Reject credential-shaped entries by
        # name before taking the trusted baseline; keep runtime secrets in an
        # external environment/configuration boundary instead.
        try:
            sensitive_active = _sensitive_entries(self.active_root)
        except CandidateIsolationError as exc:
            raise PromotionError("active_root contains an unsafe filesystem entry") from exc
        if sensitive_active:
            raise ProtectedFileError(
                "active_root contains credential-shaped paths; move private material outside the active tree"
            )
        # Never open a credential-looking path merely because it was listed as
        # protected.  If it exists, fail closed; if absent, it remains a name
        # that cannot be introduced by a candidate.
        for relative in self.protected_paths:
            target = self.active_root / Path(relative)
            if target.exists() and _is_sensitive_name(relative):
                raise ProtectedFileError(f"credential/private path cannot be inspected: {target}")
        self._protected_hashes = self._protected_snapshot()
        self._active_fingerprint = directory_fingerprint(
            self.active_root,
            budget=self.resource_budget,
        )

    def _manifest_child(
        self,
        name: str | None,
        *,
        kind: str,
        allow_missing: bool = True,
    ) -> Path | None:
        """Resolve and validate a manifest-owned sibling directory."""

        if name is None:
            return None
        if name != Path(name).name:
            raise PromotionError("promotion manifest contains an invalid sibling name")
        expected_prefix = {
            "backup": ".brain-memory-backup-",
            "stage": ".brain-memory-stage-",
            "current": ".brain-memory-rollback-current-",
        }.get(kind)
        name_pattern = (
            _MANIFEST_STAGE_NAME_RE
            if kind == "stage"
            else _MANIFEST_NAME_RE
        )
        if expected_prefix is None or not name_pattern.fullmatch(name) or not name.startswith(expected_prefix):
            raise PromotionError("promotion manifest sibling kind does not match its name")
        parent = self.active_root.parent.resolve()
        path = self.active_root.parent / name
        try:
            if path.resolve(strict=False).parent != parent:
                raise PromotionError("promotion manifest sibling escaped its root parent")
        except OSError as exc:
            raise PromotionError("cannot resolve promotion manifest sibling") from exc
        if _is_link_like(path):
            raise PromotionError("promotion manifest sibling cannot be a symlink")
        if not _lexists(path):
            if allow_missing:
                return path
            raise PromotionError(f"promotion manifest sibling is missing: {path.name}")
        if kind in {"backup", "stage", "current"} and not path.is_dir():
            raise PromotionError("promotion manifest sibling must be a directory")
        return path

    def _manifest_paths(
        self, manifest: PromotionManifest
    ) -> tuple[Path | None, Path | None, Path | None]:
        if manifest.root_digest != self._root_digest:
            raise PromotionError("promotion manifest belongs to a different active root")
        backup = self._manifest_child(manifest.backup_name, kind="backup")
        stage = self._manifest_child(manifest.stage_name, kind="stage")
        current = self._manifest_child(manifest.current_name, kind="current")
        return backup, stage, current

    def _assert_transaction_sensitive_free(
        self,
        manifest: PromotionManifest | None,
    ) -> None:
        """Reject private-looking trees before any recovery rename/removal.

        Fingerprints intentionally omit credential-shaped names so the
        evaluator never opens them.  Recovery must nevertheless inspect the
        names before a whole-tree replacement, otherwise a legacy manifest
        could move a tree containing ``runtime/.env`` into a cleanup path and
        lose it before the post-recovery policy check runs.
        """

        paths: list[Path] = [self.active_root]
        if manifest is not None:
            backup, stage, current = self._manifest_paths(manifest)
            paths.extend(path for path in (backup, stage, current) if path is not None)
        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            if _is_link_like(path):
                raise PromotionError("transaction path is a symlink/junction")
            if not _lexists(path):
                continue
            seen.add(path)
            try:
                managed = _external_managed_entries(path)
            except CandidateIsolationError as exc:
                raise PromotionError("transaction path cannot be inspected safely") from exc
            if managed:
                raise PromotionError(
                    "transaction path contains VCS/dependency metadata outside the runtime tree: "
                    + ", ".join(managed)
                )
            try:
                mutable_state = _mutable_state_entries(path)
            except CandidateIsolationError as exc:
                raise PromotionError("transaction path cannot be inspected safely") from exc
            if mutable_state:
                raise PromotionError(
                    "transaction path contains mutable database state; keep SQLite files outside the runtime tree: "
                    + ", ".join(mutable_state)
                )
            try:
                found = _sensitive_entries(path)
            except CandidateIsolationError as exc:
                raise PromotionError("transaction path cannot be inspected safely") from exc
            if found:
                raise ProtectedFileError(
                    "transaction path contains credential-shaped entries; recovery is halted"
                )

    def _owned_sibling_names(self) -> tuple[str, ...]:
        """List names reserved by this controller family without following links."""

        names: list[str] = []
        parent = self.active_root.parent
        try:
            entries = os.scandir(parent)
        except OSError as exc:
            raise PromotionError("cannot inspect the active root parent") from exc
        with entries:
            for entry in entries:
                name = entry.name
                if (
                    name.startswith((
                        ".brain-memory-backup-",
                        ".brain-memory-rollback-current-",
                        ".brain-memory-stage-",
                        ".brain-memory-failed-current-",
                        ".brain-memory-manifest-tmp-",
                    ))
                ):
                    names.append(name)
        return tuple(sorted(names))

    def _assert_no_unreferenced_siblings(
        self, manifest: PromotionManifest | None
    ) -> None:
        """Fail closed on an owned sibling left behind by a crashed writer.

        An unreferenced backup is not harmless clutter: accepting a new
        promotion would make the only known rollback point ambiguous.  The
        caller may remove it only through an explicit, audited operation (or
        manual operator intervention after this error is surfaced).
        """

        allowed = {
            name
            for name in (
                manifest.backup_name if manifest is not None else None,
                manifest.stage_name if manifest is not None else None,
                manifest.current_name if manifest is not None else None,
            )
            if name is not None
        }
        # A failed in-process swap may retain a retired-current sibling until
        # ``close()`` performs its narrow, owned cleanup.  It is known to this
        # live controller, so it is not an orphan; a fresh controller has an
        # empty list and will still fail closed on the same path after a crash.
        allowed.update(
            path.name
            for path in getattr(self, "_retired_paths", ())
            if isinstance(path, Path)
        )
        orphaned = [name for name in self._owned_sibling_names() if name not in allowed]
        if orphaned:
            raise PromotionError(
                "unreferenced promotion transaction paths require operator review: "
                + ", ".join(orphaned)
            )

    def _assert_tree_fingerprint(
        self,
        path: Path,
        expected: str | None,
        label: str,
    ) -> None:
        if expected is None:
            return
        try:
            actual = directory_fingerprint(path, budget=self.resource_budget)
        except Exception as exc:
            raise PromotionError(f"{label} cannot be fingerprinted") from exc
        if actual != expected:
            raise PromotionError(f"{label} fingerprint does not match manifest")

    def _persist_manifest(self, manifest: PromotionManifest) -> None:
        if manifest.root_digest != self._root_digest:
            raise PromotionError("promotion manifest root identity does not match controller")
        if self._manifest_path != _manifest_path_for(self.active_root):
            raise PromotionError("promotion manifest path is not controller-owned")
        _write_promotion_manifest(self._manifest_path, manifest)

    def _matching_promoted_event(
        self,
        manifest: PromotionManifest,
        *,
        reload: bool = True,
    ) -> PromotionEvent | None:
        """Find the durable promotion proof corresponding to a manifest."""

        if reload:
            reload_ledger = getattr(self.ledger, "reload", None)
            if callable(reload_ledger):
                reload_ledger()
        expected_before = manifest.before_fingerprint
        expected_after = manifest.after_fingerprint
        expected_candidate = manifest.candidate_fingerprint
        # A rollback-in-flight record swaps the before/after meanings while
        # it describes the temporary current/target pair.
        if manifest.phase == _MANIFEST_PHASE_ROLLBACK_STARTED:
            expected_before, expected_after = expected_after, expected_before
            expected_candidate = expected_candidate or manifest.before_fingerprint
        for event in reversed(self.ledger.events):
            if event.action != "promoted":
                continue
            # A hash-chain-valid record is not by itself an authorization
            # proof.  Recovery may adopt a filesystem swap only when the
            # original event explicitly carried the host write capability.
            if event.authorized is not True:
                continue
            if manifest.event_hash is not None and event.event_hash != manifest.event_hash:
                continue
            if event.metadata.get("promotion_operation_id") != manifest.operation_id:
                continue
            if manifest.receipt_hash is None or event.evaluation_receipt_hash != manifest.receipt_hash:
                continue
            if not all(
                _is_sha256(value)
                for value in (
                    event.candidate_fingerprint,
                    event.active_before_fingerprint,
                    event.active_after_fingerprint,
                    event.evaluation_receipt_hash,
                )
            ):
                continue
            if expected_candidate is not None and event.candidate_fingerprint != expected_candidate:
                continue
            if expected_before is not None and event.active_before_fingerprint != expected_before:
                continue
            if expected_after is not None and event.active_after_fingerprint != expected_after:
                continue
            if manifest.phase != _MANIFEST_PHASE_ROLLBACK_STARTED and manifest.mode is not None and event.mode != manifest.mode:
                continue
            return event
        return None

    def _matching_discard_started_event(
        self,
        manifest: PromotionManifest,
        *,
        reload: bool = True,
    ) -> PromotionEvent | None:
        """Find the explicit, authorized beginning of rollback-point discard."""

        if reload:
            reload_ledger = getattr(self.ledger, "reload", None)
            if callable(reload_ledger):
                reload_ledger()
        for event in reversed(self.ledger.events):
            if event.action != "rollback_discard_started":
                continue
            if event.authorized is not True or event.mode != PromotionMode.RECOVERY.value:
                continue
            if event.metadata.get("promotion_operation_id") != manifest.operation_id:
                continue
            if event.active_before_fingerprint != manifest.after_fingerprint:
                continue
            if event.active_after_fingerprint != manifest.after_fingerprint:
                continue
            if any(
                value is not None
                for value in (
                    event.candidate_revision_id,
                    event.candidate_fingerprint,
                    event.evaluation_receipt_hash,
                )
            ):
                continue
            return event
        return None

    def _matching_discard_event(
        self,
        manifest: PromotionManifest,
        *,
        reload: bool = True,
    ) -> PromotionEvent | None:
        """Find the durable proof for an irreversible rollback discard."""

        if reload:
            reload_ledger = getattr(self.ledger, "reload", None)
            if callable(reload_ledger):
                reload_ledger()
        expected_hash = manifest.event_hash
        for event in reversed(self.ledger.events):
            if event.action != "rollback_discarded":
                continue
            if expected_hash is None or event.event_hash != expected_hash:
                continue
            if event.authorized is not True or event.mode != PromotionMode.RECOVERY.value:
                continue
            if event.metadata.get("promotion_operation_id") != manifest.operation_id:
                continue
            if event.active_before_fingerprint != manifest.after_fingerprint:
                continue
            if event.active_after_fingerprint != manifest.after_fingerprint:
                continue
            if any(
                value is not None
                for value in (
                    event.candidate_revision_id,
                    event.candidate_fingerprint,
                    event.evaluation_receipt_hash,
                )
            ):
                continue
            return event
        return None

    def _matching_rollback_event(
        self,
        manifest: PromotionManifest,
        *,
        reload: bool = True,
    ) -> PromotionEvent | None:
        """Find the durable rollback proof for a rollback transaction.

        ``ROLLBACK_STARTED`` still stores the *promotion* event hash in
        ``manifest.event_hash``.  ``ROLLED_BACK`` replaces that field with the
        rollback event hash.  Consequently the hash check is intentionally
        phase-aware instead of treating the two records as interchangeable.
        """

        if reload:
            reload_ledger = getattr(self.ledger, "reload", None)
            if callable(reload_ledger):
                reload_ledger()
        expected_hash = (
            manifest.event_hash
            if manifest.phase == _MANIFEST_PHASE_ROLLED_BACK
            else None
        )
        for event in reversed(self.ledger.events):
            if event.action != "rolled_back":
                continue
            if expected_hash is not None and event.event_hash != expected_hash:
                continue
            if event.metadata.get("promotion_operation_id") != manifest.operation_id:
                continue
            if event.mode != PromotionMode.RECOVERY.value or event.authorized is not True:
                continue
            if event.active_before_fingerprint != manifest.before_fingerprint:
                continue
            if event.active_after_fingerprint != manifest.after_fingerprint:
                continue
            if event.candidate_revision_id is not None:
                continue
            if event.candidate_fingerprint is not None:
                continue
            if event.evaluation_receipt_hash is not None:
                continue
            return event
        return None

    def _remove_manifest_owned(
        self,
        path: Path | None,
        *,
        expected_fingerprint: str | None = None,
        label: str = "manifest sibling",
    ) -> None:
        if path is None:
            return
        # `_manifest_child` already checked the name/parent/type.  Keep the
        # final operation narrow and never follow a link introduced after the
        # check.  A path name is not an ownership proof: a crashed process can
        # leave the name available for an unrelated directory.  Every durable
        # cleanup therefore supplies the content fingerprint recorded in the
        # manifest; missing legacy fingerprints fail closed instead of risking
        # deletion of a replacement.
        if _is_link_like(path):
            raise PromotionError("refusing to remove a foreign manifest sibling")
        if not _lexists(path):
            return
        if path.parent.resolve() != self.active_root.parent.resolve():
            raise PromotionError("refusing to remove a foreign manifest sibling")
        if expected_fingerprint is None:
            raise PromotionError(f"{label} has no durable ownership fingerprint")
        self._assert_tree_fingerprint(path, expected_fingerprint, label)
        _safe_owned_remove(path, parent=self.active_root.parent)

    def _restore_manifest_backup(
        self,
        backup: Path,
        *,
        stage: Path | None = None,
        stage_fingerprint: str | None = None,
        current: Path | None = None,
        expected_fingerprint: str | None = None,
        retired_fingerprint: str | None = None,
    ) -> None:
        """Restore a known backup and discard only owned transient trees."""

        if _is_link_like(backup) or not _lexists(backup) or not backup.is_dir():
            raise PromotionError("promotion backup is unavailable for recovery")
        # Validate the rollback source before changing or deleting anything.
        # A forged/tampered backup must remain where it is so the operator can
        # inspect it; it must never replace the active tree first and only fail
        # a fingerprint check afterwards.
        if expected_fingerprint is not None:
            try:
                backup_fingerprint = directory_fingerprint(
                    backup,
                    budget=self.resource_budget,
                )
            except Exception as exc:
                raise PromotionError("promotion backup cannot be fingerprinted") from exc
            if backup_fingerprint != expected_fingerprint:
                raise PromotionError("promotion backup fingerprint does not match manifest")
        for transient in (stage, current):
            if transient is not None and _lexists(transient):
                if _is_link_like(transient) or not transient.is_dir():
                    raise PromotionError("promotion transient path has an unsafe shape")
        # Bind the path that will become ``retired`` before the first rename.
        # It contains the candidate after a completed/partially completed
        # swap, not the old tree held in ``backup``.  Confusing these two
        # fingerprints was the subtle source of both unsafe cleanup and false
        # recovery failures.
        if _is_link_like(self.active_root) or self.active_root.exists():
            if _is_link_like(self.active_root) or not self.active_root.is_dir():
                raise PromotionError("active root has an unsafe shape during recovery")
            if current is not None and _lexists(current):
                raise PromotionError("promotion recovery current path is already occupied")
            if retired_fingerprint is None:
                try:
                    retired_fingerprint = directory_fingerprint(
                        self.active_root,
                        budget=self.resource_budget,
                    )
                except Exception as exc:
                    raise PromotionError("retired active tree cannot be fingerprinted") from exc
            self._assert_tree_fingerprint(
                self.active_root,
                retired_fingerprint,
                "retired active tree before recovery",
            )
        elif current is not None and _lexists(current) and retired_fingerprint is not None:
            self._assert_tree_fingerprint(
                current,
                retired_fingerprint,
                "retired active tree before recovery",
            )
        retired = current
        try:
            if self.active_root.exists():
                retired = retired or self._new_sibling("rollback-current")
                os.replace(self.active_root, retired)
                self._assert_tree_fingerprint(
                    retired,
                    retired_fingerprint,
                    "retired active tree after rename",
                )
                _fsync_directory(self.active_root.parent)
            os.replace(backup, self.active_root)
            _fsync_directory(self.active_root.parent)
        except Exception as exc:
            # If only the first rename succeeded, restore the prior active
            # tree.  Do not discard the backup or any stage evidence.
            try:
                if not _lexists(self.active_root) and retired is not None and _lexists(retired):
                    os.replace(retired, self.active_root)
                    _fsync_directory(self.active_root.parent)
            except Exception:
                pass
            raise PromotionError("promotion backup could not be restored") from exc
        # The source was verified before the swap, but fingerprint the active
        # name again to detect a host-level rename race before cleanup.
        if expected_fingerprint is not None:
            try:
                actual = directory_fingerprint(
                    self.active_root,
                    budget=self.resource_budget,
                )
            except Exception as exc:
                raise PromotionError("recovered active tree cannot be fingerprinted") from exc
            if actual != expected_fingerprint:
                raise PromotionError("recovered active tree disagrees with verified backup")
        if stage is not None and _lexists(stage):
            self._remove_manifest_owned(
                stage,
                expected_fingerprint=stage_fingerprint,
                label="promotion staging tree",
            )
        if retired is not None and _lexists(retired):
            self._remove_manifest_owned(
                retired,
                expected_fingerprint=retired_fingerprint,
                label="retired active tree",
            )

    def _committed_manifest_after_rollback(
        self,
        manifest: PromotionManifest,
        promoted_event: PromotionEvent,
    ) -> PromotionManifest:
        """Convert a rollback-in-flight record back to its committed shape."""

        # Rollback records temporarily use before=current-candidate and
        # after=rollback-target.  Once rollback is abandoned/interrupted, the
        # original promotion contract must be restored exactly so subsequent
        # receipt matching remains possible.
        return manifest.with_phase(
            _MANIFEST_PHASE_COMMITTED,
            current_name=None,
            before_fingerprint=manifest.after_fingerprint,
            after_fingerprint=manifest.before_fingerprint,
            candidate_fingerprint=(
                manifest.candidate_fingerprint or manifest.before_fingerprint
            ),
            mode=promoted_event.mode,
            event_hash=promoted_event.event_hash,
        )

    def _recover_promotion_manifest(self) -> None:
        """Reconcile an interrupted swap before the controller fingerprints root."""

        manifest = _read_promotion_manifest(self._manifest_path)
        if manifest is None:
            self._assert_transaction_sensitive_free(None)
            return
        self._assert_transaction_sensitive_free(manifest)
        backup, stage, current = self._manifest_paths(manifest)
        phase = manifest.phase
        active_exists = _lexists(self.active_root) and not _is_link_like(self.active_root)
        backup_exists = backup is not None and _lexists(backup)
        stage_exists = stage is not None and _lexists(stage)
        current_exists = current is not None and _lexists(current)

        if phase == _MANIFEST_PHASE_IDLE:
            if backup_exists or stage_exists or current_exists:
                raise PromotionError("idle promotion manifest has unexpected transaction paths")
            return

        if phase in {_MANIFEST_PHASE_PREPARED, _MANIFEST_PHASE_ACTIVE_MOVED, _MANIFEST_PHASE_SWAPPED}:
            if phase in {_MANIFEST_PHASE_PREPARED, _MANIFEST_PHASE_ACTIVE_MOVED} and not backup_exists:
                # The manifest is written before the first rename.  A crash
                # in this narrow window leaves the trusted active tree and
                # stage side by side; discard only the owned stage and return
                # to IDLE when the active fingerprint still matches.
                if active_exists and stage_exists and not current_exists:
                    if manifest.before_fingerprint is not None:
                        actual = directory_fingerprint(
                            self.active_root,
                            budget=self.resource_budget,
                        )
                        if actual != manifest.before_fingerprint:
                            raise PromotionError("prepared promotion active tree drifted")
                    self._remove_manifest_owned(
                        stage,
                        expected_fingerprint=manifest.stage_fingerprint,
                        label="prepared promotion staging tree",
                    )
                    self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    return
                raise PromotionError("prepared promotion has an ambiguous filesystem shape")
            if backup is None or not backup_exists:
                raise PromotionError("interrupted promotion is missing its backup")
            promoted_event = self._matching_promoted_event(manifest)
            if phase == _MANIFEST_PHASE_SWAPPED and promoted_event is not None:
                # The receipt may have reached durable storage immediately
                # before the COMMITTED manifest write.  Adopt the candidate
                # and retain the rollback point; no duplicate event is made.
                if stage_exists or current_exists:
                    raise PromotionError("committed promotion retains unexpected transient paths")
                self._assert_tree_fingerprint(
                    backup, manifest.before_fingerprint, "promotion backup"
                )
                self._assert_tree_fingerprint(
                    self.active_root, manifest.after_fingerprint, "active promotion tree"
                )
                committed = manifest.with_phase(
                    _MANIFEST_PHASE_COMMITTED,
                    event_hash=promoted_event.event_hash,
                )
                self._persist_manifest(committed)
                self._backup_path = backup
                return
            # PREPARED/ACTIVE_MOVED, or SWAPPED without a matching receipt,
            # has no durable proof of success.  Restore the old tree and throw
            # away only the owned stage/current siblings.
            if active_exists and current is None:
                current = self._new_sibling("rollback-current")
                manifest = manifest.with_phase(
                    phase,
                    current_name=current.name,
                )
                self._persist_manifest(manifest)
            self._restore_manifest_backup(
                backup,
                stage=stage,
                stage_fingerprint=manifest.stage_fingerprint,
                current=current,
                expected_fingerprint=manifest.before_fingerprint,
                retired_fingerprint=(
                    manifest.after_fingerprint or manifest.stage_fingerprint
                ),
            )
            self._persist_manifest(PromotionManifest.idle(self._root_digest))
            self._backup_path = None
            return

        if phase == _MANIFEST_PHASE_COMMITTED:
            if backup is None or not backup_exists:
                raise PromotionError("committed promotion lost its rollback backup")
            if not active_exists:
                raise PromotionError("committed promotion has no active tree")
            promoted_event = self._matching_promoted_event(manifest)
            if promoted_event is None:
                raise PromotionError("committed manifest has no matching promotion receipt")
            self._assert_tree_fingerprint(
                backup, manifest.before_fingerprint, "promotion rollback backup"
            )
            if manifest.after_fingerprint is not None:
                self._assert_tree_fingerprint(
                    self.active_root, manifest.after_fingerprint, "active committed tree"
                )
            if stage_exists or current_exists:
                raise PromotionError("committed promotion retains unexpected transient paths")
            self._backup_path = backup
            return

        if phase == _MANIFEST_PHASE_ROLLED_BACK:
            if backup_exists or stage_exists or not active_exists:
                raise PromotionError("rolled-back manifest has an unsafe filesystem shape")
            self._assert_tree_fingerprint(
                self.active_root, manifest.after_fingerprint, "rolled-back active tree"
            )
            rollback_event = self._matching_rollback_event(manifest)
            if rollback_event is None:
                raise PromotionError("rolled-back manifest lacks a durable receipt")
            if current_exists:
                self._remove_manifest_owned(
                    current,
                    expected_fingerprint=manifest.before_fingerprint,
                    label="rolled-back current tree",
                )
                _fsync_directory(self.active_root.parent)
            self._persist_manifest(PromotionManifest.idle(self._root_digest))
            self._backup_path = None
            return

        if phase == _MANIFEST_PHASE_ROLLBACK_STARTED:
            # A rollback is authorized but still needs the same crash-safe
            # reconciliation.  If the second rename completed, active is the
            # old tree and `current` is the former candidate; otherwise restore
            # the pre-rollback candidate and keep the backup available.
            promoted_event = self._matching_promoted_event(manifest)
            if promoted_event is None:
                raise PromotionError("rollback manifest has no matching promotion receipt")
            if backup_exists and not active_exists and current_exists:
                self._assert_tree_fingerprint(
                    backup, manifest.after_fingerprint, "rollback target backup"
                )
                if manifest.before_fingerprint is not None:
                    self._assert_tree_fingerprint(
                        current, manifest.before_fingerprint, "rollback current tree"
                    )
                os.replace(current, self.active_root)
                _fsync_directory(self.active_root.parent)
                self._persist_manifest(
                    self._committed_manifest_after_rollback(manifest, promoted_event)
                )
                self._backup_path = backup
                return
            if not backup_exists and active_exists and current_exists:
                # The old tree is active and the candidate is retired.  The
                # rollback receipt may or may not have been appended; inspect
                # the ledger before accepting the state.
                rollback_event = self._matching_rollback_event(manifest)
                if rollback_event is None:
                    # Both renames completed but the rollback ledger append
                    # did not.  Restore the candidate+backup pair so the
                    # original committed promotion remains available.
                    if manifest.after_fingerprint is not None:
                        self._assert_tree_fingerprint(
                            self.active_root, manifest.after_fingerprint, "rollback target tree"
                        )
                    if manifest.before_fingerprint is not None:
                        self._assert_tree_fingerprint(
                            current, manifest.before_fingerprint, "rollback current tree"
                        )
                    if backup is None:
                        raise PromotionError("rollback manifest has no backup name")
                    os.replace(self.active_root, backup)
                    _fsync_directory(self.active_root.parent)
                    os.replace(current, self.active_root)
                    _fsync_directory(self.active_root.parent)
                    self._persist_manifest(
                        self._committed_manifest_after_rollback(manifest, promoted_event)
                    )
                    self._backup_path = backup
                    return
                self._assert_tree_fingerprint(
                    self.active_root, manifest.after_fingerprint, "completed rollback target tree"
                )
                if manifest.before_fingerprint is not None:
                    self._assert_tree_fingerprint(
                        current, manifest.before_fingerprint, "completed rollback current tree"
                    )
                self._remove_manifest_owned(
                    current,
                    expected_fingerprint=manifest.before_fingerprint,
                    label="completed rollback current tree",
                )
                _fsync_directory(self.active_root.parent)
                self._persist_manifest(PromotionManifest.idle(self._root_digest))
                self._backup_path = None
                return
            if not backup_exists and active_exists and not current_exists:
                # The retired candidate was already removed and only the
                # final manifest cleanup was interrupted.  A durable
                # rolled_back event is required before accepting this shape.
                self._assert_tree_fingerprint(
                    self.active_root, manifest.after_fingerprint, "completed rollback active tree"
                )
                rollback_event = self._matching_rollback_event(manifest)
                if rollback_event is None:
                    raise PromotionError("rollback completed without a durable receipt")
                self._persist_manifest(PromotionManifest.idle(self._root_digest))
                self._backup_path = None
                return
            if backup_exists and active_exists and not current_exists:
                # Crash before the first rollback rename.
                self._assert_tree_fingerprint(
                    backup, manifest.after_fingerprint, "rollback target backup"
                )
                self._assert_tree_fingerprint(
                    self.active_root, manifest.before_fingerprint, "active rollback candidate"
                )
                self._persist_manifest(
                    self._committed_manifest_after_rollback(manifest, promoted_event)
                )
                self._backup_path = backup
                return
            raise PromotionError("rollback manifest has an ambiguous filesystem shape")

        if phase == _MANIFEST_PHASE_FINALIZING:
            # Finalization is intentionally conservative: a crash before the
            # backup unlink leaves the rollback point intact; after unlink the
            # ledger must contain the explicit discard event before we clear
            # the manifest.
            if not active_exists:
                raise PromotionError("rollback finalization has no active tree")
            if manifest.after_fingerprint is not None:
                self._assert_tree_fingerprint(
                    self.active_root, manifest.after_fingerprint, "finalizing active tree"
                )
            if backup_exists:
                if manifest.before_fingerprint is not None:
                    self._assert_tree_fingerprint(
                        backup, manifest.before_fingerprint, "finalizing rollback backup"
                    )
                started_event = self._matching_discard_started_event(manifest)
                if started_event is None:
                    raise PromotionError("rollback finalization has no explicit start proof")
                self._backup_path = backup
                return
            # A FINALIZING record without its backup is only acceptable when
            # the explicit discard event is already durable.  Otherwise a
            # crash would silently turn an unrecorded destructive cleanup into
            # a successful startup.
            discard_event = self._matching_discard_event(manifest)
            if discard_event is None:
                raise PromotionError("finalizing promotion lacks durable discard proof")
            self._persist_manifest(PromotionManifest.idle(self._root_digest))
            self._backup_path = None
            return

        raise PromotionError("promotion manifest phase cannot be recovered")

    def _refresh_manifest_state(self) -> PromotionManifest | None:
        """Refresh the in-memory rollback pointer under the root lock."""

        manifest = _read_promotion_manifest(self._manifest_path)
        self._assert_no_unreferenced_siblings(manifest)
        self._assert_transaction_sensitive_free(manifest)
        if manifest is None:
            self._backup_path = None
            return None
        if manifest.phase == _MANIFEST_PHASE_IDLE:
            self._backup_path = None
            return manifest
        if manifest.phase == _MANIFEST_PHASE_COMMITTED:
            backup, stage, current = self._manifest_paths(manifest)
            if (
                backup is None
                or not _lexists(backup)
                or (stage is not None and _lexists(stage))
                or (current is not None and _lexists(current))
            ):
                raise PromotionError("committed promotion manifest is not reconciled")
            self._assert_tree_fingerprint(
                backup, manifest.before_fingerprint, "promotion rollback backup"
            )
            self._assert_tree_fingerprint(
                self.active_root, manifest.after_fingerprint, "active committed tree"
            )
            self._backup_path = backup
            return manifest
        # A second controller can encounter a transaction written by another
        # process after this object was constructed.  Reconcile it before
        # deciding whether a new promote is allowed.
        self._recover_promotion_manifest()
        # Recovery has already validated the resulting directory shape and
        # fingerprints.  Refresh only after a non-idle transaction; doing
        # this for an ordinary idle call would turn an external edit into a
        # new trusted baseline and defeat the controller's drift check.
        try:
            self._active_fingerprint = directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )
        except Exception as exc:
            raise PromotionError("recovered active tree cannot be fingerprinted") from exc
        return _read_promotion_manifest(self._manifest_path)

    def _verify_manifest_state(self) -> bool:
        """Read-only validation used by :meth:`verify` (never repairs state)."""

        manifest = _read_promotion_manifest(self._manifest_path)
        self._assert_no_unreferenced_siblings(manifest)
        self._assert_transaction_sensitive_free(manifest)
        if manifest is None:
            return self._backup_path is None
        backup, stage, current = self._manifest_paths(manifest)
        if manifest.phase == _MANIFEST_PHASE_IDLE:
            return (
                self._backup_path is None
                and (backup is None or not _lexists(backup))
                and (stage is None or not _lexists(stage))
                and (current is None or not _lexists(current))
            )
        if manifest.phase != _MANIFEST_PHASE_COMMITTED:
            return False
        if (
            backup is None
            or not _lexists(backup)
            or (stage is not None and _lexists(stage))
            or (current is not None and _lexists(current))
        ):
            return False
        if self._backup_path is None or self._backup_path != backup:
            return False
        if not self.active_root.is_dir() or _is_link_like(self.active_root):
            return False
        self._assert_tree_fingerprint(backup, manifest.before_fingerprint, "promotion rollback backup")
        self._assert_tree_fingerprint(self.active_root, manifest.after_fingerprint, "active committed tree")
        # ``verify`` is intentionally observational.  The ledger has already
        # been checked above, so do not call ``reload()`` here (which would
        # mutate a file-backed ledger object's in-memory cache).
        return self._matching_promoted_event(manifest, reload=False) is not None

    @property
    def active_path(self) -> Path:
        return self.active_root

    @property
    def manifest_path(self) -> Path:
        """The external transaction manifest used for crash recovery."""

        return self._manifest_path

    @property
    def active_fingerprint(self) -> str:
        with self._lock:
            return directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )

    @property
    def current_fingerprint(self) -> str:
        return self.active_fingerprint

    @property
    def rollback_available(self) -> bool:
        with self._lock:
            return (
                self._backup_path is not None
                and _lexists(self._backup_path)
                and not _is_link_like(self._backup_path)
            )

    @property
    def last_outcome(self) -> PromotionOutcome | None:
        return self._last_outcome

    @property
    def protected_hashes(self) -> Mapping[str, str]:
        return dict(self._protected_hashes)

    def _protected_snapshot(self, root: Path | None = None) -> dict[str, str]:
        target = root or self.active_root
        return {
            relative: _path_digest(target, relative, budget=self.resource_budget)
            for relative in self.protected_paths
        }

    def _candidate_protected_violations(self, candidate: Path) -> tuple[str, ...]:
        violations: list[str] = []
        for relative in self.protected_paths:
            active_digest = self._protected_hashes.get(relative, _MISSING)
            candidate_path = candidate / Path(relative)
            if not candidate_path.exists() and not _is_link_like(candidate_path):
                continue
            if _is_sensitive_name(relative):
                violations.append(relative)
                continue
            try:
                if candidate_path.is_dir():
                    _validate_protected_directory(candidate_path, budget=self.resource_budget)
                candidate_digest = _path_digest(
                    candidate,
                    relative,
                    budget=self.resource_budget,
                )
            except PromotionError:
                violations.append(relative)
                continue
            # A missing active path may be introduced only by the host's
            # explicit protected-path policy, never by a candidate.
            if active_digest == _MISSING or candidate_digest != active_digest:
                violations.append(relative)
        return tuple(violations)

    @staticmethod
    def _same_or_nested(first: Path, second: Path) -> bool:
        try:
            first.relative_to(second)
            return True
        except ValueError:
            pass
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False

    def _assert_ledger_location(self, path: Path) -> None:
        """Keep the audit ledger and its lock outside controller metadata."""

        path = Path(path)
        forbidden = {
            self._manifest_path,
            self._controller_lock_path,
            Path(str(self._manifest_path) + ".lock"),
            Path(str(self._controller_lock_path) + ".lock"),
        }
        normalized = os.path.normcase(str(path))
        if any(normalized == os.path.normcase(str(item)) for item in forbidden):
            raise PromotionError("promotion ledger collides with controller metadata")
        if path.name.startswith((
            ".brain-memory-backup-",
            ".brain-memory-rollback-current-",
            ".brain-memory-stage-",
            ".brain-memory-failed-current-",
            ".brain-memory-controller-",
        )):
            raise PromotionError("promotion ledger uses a controller-reserved name")
        # Resolve aliases that already exist (hard links, junctions, or an
        # unexpected symlink) before the first append.  ``PromotionLedger``
        # separately rejects symlink/hard-link files; this check also catches
        # an alias to the manifest/lock itself.
        if path.exists():
            for item in forbidden:
                try:
                    if path.samefile(item):
                        raise PromotionError("promotion ledger aliases controller metadata")
                except FileNotFoundError:
                    continue
                except OSError:
                    continue

    def _validate_persistence_location(
        self,
        value: str | os.PathLike[str] | Path,
    ) -> Path:
        """Resolve a database path and keep it outside every swap-owned tree.

        The active-tree scan catches files that already exist, but SQLite may
        create its main file and WAL lazily on the first write.  A host that
        binds its persistent store here therefore gets a deterministic failure
        before promotion even when the path does not exist yet.  Requiring an
        absolute path also prevents a later working-directory change from
        silently relocating the life ledger.
        """

        try:
            raw = os.fspath(value)
        except TypeError as exc:
            raise PromotionError("persistence_path must be a local filesystem path") from exc
        if not Path(raw).is_absolute():
            raise PromotionError("persistence_path must be an absolute local path")
        path = _resolve_local(raw, strict=False)
        if self._same_or_nested(path, self.active_root):
            raise PromotionError(
                "persistence_path must be outside the atomically swapped active tree"
            )
        # Transaction siblings are controller-owned evidence.  Keeping a
        # database out of that namespace avoids a future cleanup/recovery pass
        # mistaking mutable state for a backup or stage.
        try:
            relative_to_parent = path.relative_to(self.active_root.parent)
        except ValueError:
            relative_to_parent = None
        if relative_to_parent is not None and relative_to_parent.parts:
            first = relative_to_parent.parts[0]
            if first.startswith(".brain-memory-"):
                raise PromotionError(
                    "persistence_path cannot use a controller transaction sibling"
                )
        forbidden = {
            self._manifest_path,
            self._controller_lock_path,
            Path(str(self._manifest_path) + ".lock"),
            Path(str(self._controller_lock_path) + ".lock"),
        }
        if any(os.path.normcase(str(path)) == os.path.normcase(str(item)) for item in forbidden):
            raise PromotionError("persistence_path collides with controller metadata")
        return path

    def bind_persistence_path(self, value: str | os.PathLike[str] | Path) -> Path:
        """Bind the host's durable SQLite path before a promotion can occur."""

        path = self._validate_persistence_location(value)
        if self.persistence_path is not None and self.persistence_path != path:
            raise PromotionError("persistence_path cannot be rebound on a live controller")
        self.persistence_path = path
        return path

    def _candidate_path(self, candidate: str | os.PathLike[str] | CandidateRevision) -> Path:
        source = candidate.source_path if isinstance(candidate, CandidateRevision) else candidate
        path = _resolve_local(source, strict=True)
        if _is_link_like(path) or not path.is_dir():
            raise CandidateIsolationError("candidate must be a real local directory")
        if self._same_or_nested(path, self.active_root):
            raise CandidateIsolationError("candidate and active tree must be separate siblings")
        externally_managed = _external_managed_entries(path)
        if externally_managed:
            raise CandidateIsolationError(
                "candidate must be a runtime tree outside VCS/dependency metadata: "
                + ", ".join(externally_managed)
            )
        mutable_state = _mutable_state_entries(path)
        if mutable_state:
            raise CandidateIsolationError(
                "candidate contains mutable database state; keep SQLite files outside the runtime tree: "
                + ", ".join(mutable_state)
            )
        try:
            sensitive_candidate = _sensitive_entries(path)
        except CandidateIsolationError:
            raise
        if sensitive_candidate:
            raise CandidateIsolationError(
                "candidate contains credential-shaped paths; private material must stay outside the candidate"
            )
        return path

    def evaluate(
        self,
        candidate: str | os.PathLike[str] | CandidateRevision,
        baseline: BaselineRevision | Mapping[str, Any] | str | os.PathLike[str],
        *,
        mode: PromotionMode | EvaluationMode | str = EVOLUTION,
        **kwargs: Any,
    ) -> EvaluationReceipt:
        if self.harness is None:
            raise PromotionError("evaluate requires a configured EvaluationHarness")
        path = self._candidate_path(candidate)
        return self.harness.evaluate(path, baseline, mode=PromotionMode.parse(mode).value, **kwargs)

    def _authorised(self, authorized: Any = None, host: Any = None) -> bool:
        if authorized is True:
            return True
        if authorized not in (None, False):
            return False
        if host is not None:
            if isinstance(host, Mapping):
                return host.get("authorized") is True
            return getattr(host, "authorized", False) is True
        return self.host_authorized

    def _coerce_receipt(self, value: Any) -> EvaluationReceipt:
        if isinstance(value, EvaluationReceipt):
            receipt = value
        elif isinstance(value, Mapping):
            try:
                receipt = EvaluationReceipt.from_dict(value)
            except Exception as exc:
                raise ReceiptValidationError("evaluation receipt cannot be restored") from exc
        else:
            raise ReceiptValidationError(
                "candidate-owned receipt objects are not accepted; provide EvaluationReceipt"
            )
        if type(receipt) is not EvaluationReceipt:
            raise ReceiptValidationError("receipt must be the fixed EvaluationReceipt type")
        return receipt

    def _validate_receipt(
        self,
        value: Any,
        *,
        candidate_fingerprint: str,
        requested_mode: PromotionMode,
        active_fingerprint: str,
    ) -> tuple[EvaluationReceipt, str | None]:
        receipt = self._coerce_receipt(value)
        if not receipt.verify():
            raise ReceiptValidationError("evaluation receipt hash is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", receipt.receipt_hash or ""):
            raise ReceiptValidationError("evaluation receipt hash is not a SHA-256 digest")
        try:
            receipt_mode = PromotionMode.parse(receipt.mode)
        except PromotionError as exc:
            raise ReceiptValidationError("evaluation receipt mode is invalid") from exc
        if receipt_mode is not requested_mode:
            raise PromotionError(
                f"receipt mode {receipt_mode.value!r} does not match requested mode {requested_mode.value!r}"
            )
        if receipt.candidate_fingerprint != candidate_fingerprint:
            return receipt, "candidate fingerprint changed after evaluation"
        if not receipt.baseline_fingerprint:
            raise ReceiptValidationError("promotion requires a fingerprinted trusted baseline")
        if receipt.baseline_fingerprint != active_fingerprint:
            return receipt, "active baseline changed after evaluation"
        if self.harness is not None:
            contract = (
                ("harness_version", self.harness.harness_version, receipt.harness_version),
                ("fixture_hash", self.harness.fixture_hash, receipt.fixture_hash),
                ("evaluator_hash", self.harness.evaluator_hash, receipt.evaluator_hash),
            )
            for name, expected, actual in contract:
                if str(expected) != str(actual):
                    raise ReceiptValidationError(f"receipt {name} does not match fixed evaluator contract")
        else:
            for name in ("harness_version", "fixture_hash", "evaluator_hash"):
                if not getattr(receipt, name, ""):
                    raise ReceiptValidationError(f"receipt {name} is missing")
            for name, expected in (
                ("harness_version", self.harness_version),
                ("fixture_hash", self.fixture_hash),
                ("evaluator_hash", self.evaluator_hash),
            ):
                if expected is not None and str(expected) != str(getattr(receipt, name)):
                    raise ReceiptValidationError(f"receipt {name} does not match configured contract")

        names = [str(gate.name) for gate in receipt.gates]
        if len(names) != len(set(names)):
            raise ReceiptValidationError("evaluation receipt contains duplicate gates")
        missing = _REQUIRED_COMMON_GATES.difference(names)
        required_progress_gate = (
            HardGate.IMPROVEMENT.value
            if requested_mode is PromotionMode.EVOLUTION
            else HardGate.RECOVERY_BASELINE.value
        )
        if required_progress_gate not in names:
            raise ReceiptValidationError(f"evaluation receipt is missing {required_progress_gate} gate")
        gate_map = {gate.name: gate.passed for gate in receipt.gates}
        if missing:
            raise ReceiptValidationError(f"evaluation receipt is missing hard gates: {sorted(missing)}")

        # A structurally valid but rejected receipt is useful evidence and is
        # recorded as a rejection.  It must never authorize a write.
        if not receipt.accepted:
            return receipt, "; ".join(receipt.failed_gates) or "evaluator rejected candidate"
        if receipt.outcome != (
            "ACCEPTED_EVOLUTION" if requested_mode is PromotionMode.EVOLUTION else "ACCEPTED_RECOVERY"
        ):
            raise ReceiptValidationError("accepted receipt has an inconsistent outcome")
        if receipt.rollback_performed:
            raise ReceiptValidationError("a receipt that already rolled back cannot authorize promotion")
        if not receipt.isolated or not receipt.result_verified or not receipt.hard_gates_passed:
            return receipt, "one or more evaluator hard gates failed"
        if not gate_map.get(HardGate.INDEPENDENT_JUDGE.value, False):
            return receipt, "independent fixture-owned judge gate failed"
        if not gate_map.get(required_progress_gate, False):
            return receipt, f"{required_progress_gate} gate failed"
        return receipt, None

    def _validate_sandbox_attestation(
        self,
        value: SandboxAttestation | Mapping[str, Any] | None,
        *,
        receipt: EvaluationReceipt,
        candidate_fingerprint: str,
        mode: PromotionMode,
        consume: bool = False,
    ) -> SandboxAttestation:
        """Validate a host proof against the exact receipt being promoted."""

        if not self.require_sandbox_attestation:
            raise SandboxAttestationError("sandbox attestation is disabled for this profile")
        if self.sandbox_attestor is None:
            raise SandboxAttestationError(
                "production promotion requires a host SandboxAttestor"
            )
        if value is None:
            raise SandboxAttestationError(
                "production promotion requires host-issued sandbox attestation"
            )
        return self.sandbox_attestor.validate(
            value,
            candidate_fingerprint=candidate_fingerprint,
            receipt_hash=receipt.receipt_hash,
            mode=mode,
            harness_version=receipt.harness_version,
            fixture_hash=receipt.fixture_hash,
            evaluator_hash=receipt.evaluator_hash,
            required_capabilities=REQUIRED_SANDBOX_CAPABILITIES,
            consume=consume,
        )

    def _record_attestation_failure(
        self,
        *,
        mode: PromotionMode,
        receipt: EvaluationReceipt,
        candidate_revision_id: str,
        candidate_fingerprint: str,
        before: str,
        reason: str,
    ) -> PromotionEvent:
        return self._record(
            action="attestation_denied",
            mode=mode,
            receipt=receipt,
            candidate_revision_id=candidate_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            before=before,
            after=before,
            authorized=False,
            reason=reason,
            metadata={"sandbox_attestation_required": True},
        )

    def _record(
        self,
        *,
        action: str,
        mode: PromotionMode,
        receipt: EvaluationReceipt | None,
        candidate_revision_id: str | None,
        candidate_fingerprint: str | None,
        before: str | None,
        after: str | None,
        authorized: bool,
        reason: str,
        attestation: SandboxAttestation | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PromotionEvent:
        event_metadata = dict(metadata or {})
        if attestation is not None:
            event_metadata.update(
                {
                    "sandbox_attestation_id": attestation.attestation_id,
                    "sandbox_attestation_hash": attestation.attestation_hash,
                    "sandbox_capabilities": list(attestation.capabilities),
                }
            )
        # A controller may have been constructed before another process
        # appended to the same file-backed ledger.  Refresh under the ledger
        # sidecar lock before deriving the next sequence/hash so a safe
        # rejection is recorded instead of failing on stale in-memory state.
        reload_ledger = getattr(self.ledger, "reload", None)
        if callable(reload_ledger):
            reload_ledger()
        return self.ledger.append(
            action=action,
            mode=mode.value,
            candidate_revision_id=candidate_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            evaluation_receipt_hash=receipt.receipt_hash if receipt is not None else None,
            active_before_fingerprint=before,
            active_after_fingerprint=after,
            authorized=authorized,
            reason=reason,
            metadata=event_metadata,
        )

    def _outcome(
        self,
        *,
        accepted: bool,
        action: str,
        mode: PromotionMode,
        receipt: EvaluationReceipt | None,
        candidate_revision_id: str | None,
        candidate_fingerprint: str | None,
        before: str | None,
        after: str | None,
        rollback_available: bool,
        event: PromotionEvent,
        reason: str,
        attestation: SandboxAttestation | None = None,
    ) -> PromotionOutcome:
        attestation_id = attestation.attestation_id if attestation is not None else None
        attestation_hash = attestation.attestation_hash if attestation is not None else None
        result = PromotionOutcome(
            accepted=accepted,
            action=action,
            mode=mode.value,
            candidate_revision_id=candidate_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            evaluation_receipt_hash=receipt.receipt_hash if receipt is not None else None,
            active_before_fingerprint=before,
            active_after_fingerprint=after,
            rollback_available=rollback_available,
            record_hash=event.event_hash,
            reason=_bounded_text(reason, 1000),
            sandbox_attestation_id=attestation_id,
            sandbox_attestation_hash=attestation_hash,
            attestation_verified=attestation is not None,
        )
        self._last_outcome = result
        return result

    def _reject(
        self,
        *,
        mode: PromotionMode,
        receipt: EvaluationReceipt | None,
        candidate_revision_id: str | None,
        candidate_fingerprint: str | None,
        reason: str,
        action: str = "rejected",
    ) -> PromotionOutcome:
        try:
            current = directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )
        except Exception:
            current = self._active_fingerprint
        event = self._record(
            action=action,
            mode=mode,
            receipt=receipt,
            candidate_revision_id=candidate_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            before=current,
            after=current,
            authorized=False,
            reason=reason,
        )
        return self._outcome(
            accepted=False,
            action=action,
            mode=mode,
            receipt=receipt,
            candidate_revision_id=candidate_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            before=current,
            after=current,
            rollback_available=self.rollback_available,
            event=event,
            reason=reason,
        )

    def _new_sibling(self, prefix: str) -> Path:
        for _ in range(20):
            candidate = self.active_root.parent / f".brain-memory-{prefix}-{uuid.uuid4().hex}"
            if _is_link_like(candidate):
                raise PromotionError("temporary sibling name is occupied by a symlink/junction")
            if not _lexists(candidate):
                return candidate
        raise PromotionError("cannot allocate a temporary sibling path")

    def _stage_candidate(self, candidate: Path) -> Path:
        stage = Path(tempfile.mkdtemp(prefix=".brain-memory-stage-", dir=str(self.active_root.parent)))
        # mkdtemp creates the directory; _copy_tree expects a fresh path, so
        # remove only this owned empty directory before copying.
        stage.rmdir()
        copy_usage = [0, 0]
        _copy_tree(
            candidate,
            stage,
            exclude=self.protected_paths,
            budget=self.resource_budget,
            usage=copy_usage,
        )
        _copy_protected(
            self.active_root,
            stage,
            self.protected_paths,
            budget=self.resource_budget,
            usage=copy_usage,
        )
        if self._protected_snapshot(stage) != self._protected_hashes:
            _safe_owned_remove(stage, parent=self.active_root.parent)
            raise ProtectedFileError("staged tree does not preserve protected paths")
        return stage

    def _swap_in(self, stage: Path) -> Path:
        # A caller may reserve the backup name in the durable manifest before
        # the first rename.  Keep the one-argument method signature for
        # existing orchestrators/tests that wrap or monkey-patch `_swap_in`.
        backup = self._planned_backup_path or self._new_sibling("backup")
        self._planned_backup_path = None
        if backup.exists():
            raise PromotionError("promotion backup path is unexpectedly occupied")
        os.replace(self.active_root, backup)
        _fsync_directory(self.active_root.parent)
        if self._active_move_manifest is not None:
            # Persist the phase immediately after the first rename.  If the
            # host dies before this write, PREPARED plus the filesystem shape
            # still provides a conservative recovery path.
            self._persist_manifest(
                self._active_move_manifest.with_phase(_MANIFEST_PHASE_ACTIVE_MOVED)
            )
        try:
            os.replace(stage, self.active_root)
            _fsync_directory(self.active_root.parent)
        except Exception:
            try:
                expected = (
                    self._active_move_manifest.before_fingerprint
                    if self._active_move_manifest is not None
                    else None
                )
                if expected is not None:
                    self._assert_tree_fingerprint(
                        backup, expected, "promotion backup during swap recovery"
                    )
                os.replace(backup, self.active_root)
                _fsync_directory(self.active_root.parent)
                if expected is not None:
                    self._assert_tree_fingerprint(
                        self.active_root, expected, "restored active tree during swap recovery"
                    )
            except Exception as restore_exc:  # pragma: no cover - catastrophic host failure
                raise PromotionError("promotion failed and active tree could not be restored") from restore_exc
            raise
        return backup

    def _restore_backup(
        self,
        backup: Path,
        *,
        expected_fingerprint: str | None = None,
    ) -> None:
        """Restore a backup after a failed in-process promotion.

        The expected fingerprint is deliberately optional for compatibility
        with older private callers, but every promotion path supplies it.
        Checking the source before the first rename prevents a corrupted or
        replaced backup from becoming the active tree.
        """

        if _is_link_like(backup) or not _lexists(backup) or not backup.is_dir():
            raise PromotionError("promotion backup is unavailable for restoration")
        if expected_fingerprint is not None:
            self._assert_tree_fingerprint(
                backup, expected_fingerprint, "promotion restoration backup"
            )
        current = self._new_sibling("failed-current")
        moved_current = False
        retired_fingerprint: str | None = None
        try:
            if self.active_root.exists():
                if _is_link_like(self.active_root) or not self.active_root.is_dir():
                    raise PromotionError("active tree has an unsafe shape during restoration")
                retired_fingerprint = directory_fingerprint(
                    self.active_root,
                    budget=self.resource_budget,
                )
                os.replace(self.active_root, current)
                moved_current = True
                self._assert_tree_fingerprint(
                    current,
                    retired_fingerprint,
                    "retired active tree after rename",
                )
                _fsync_directory(self.active_root.parent)
            os.replace(backup, self.active_root)
            _fsync_directory(self.active_root.parent)
        except Exception as exc:
            # Best effort restoration; do not hide the original failure.
            try:
                if not _lexists(self.active_root) and _lexists(current):
                    os.replace(current, self.active_root)
                    _fsync_directory(self.active_root.parent)
            except Exception:
                pass
            raise PromotionError("active tree restoration failed") from exc
        if expected_fingerprint is not None:
            try:
                self._assert_tree_fingerprint(
                    self.active_root,
                    expected_fingerprint,
                    "restored active tree",
                )
            except Exception as exc:
                # The source was checked before the swap.  If a host race
                # still changed the restored name, put the old active tree
                # and backup name back before failing closed.
                try:
                    if _lexists(self.active_root) and not _is_link_like(self.active_root) and self.active_root.is_dir():
                        os.replace(self.active_root, backup)
                        _fsync_directory(self.active_root.parent)
                    if _lexists(current):
                        os.replace(current, self.active_root)
                        _fsync_directory(self.active_root.parent)
                except Exception:
                    pass
                raise PromotionError("restored active tree disagrees with backup") from exc
        if moved_current:
            if retired_fingerprint is None:
                # The public promotion path always supplies this value.  Do
                # not retain an unbound cleanup target for legacy/private
                # callers; leaving it visible is safer than deleting it.
                raise PromotionError("retired active tree has no ownership fingerprint")
            self._retired_paths.append(current)
            self._retired_fingerprints[current] = retired_fingerprint

    def _remove_retired_owned(self, path: Path) -> None:
        """Remove an in-process retired tree only if its identity is intact."""

        expected = self._retired_fingerprints.get(path)
        if expected is None:
            raise PromotionError("retired active tree is not bound to this controller")
        if _is_link_like(path):
            raise PromotionError("refusing to remove a foreign retired tree")
        if _lexists(path):
            if path.parent.resolve() != self.active_root.parent.resolve():
                raise PromotionError("refusing to remove a foreign retired tree")
            if _sensitive_entries(path):
                raise ProtectedFileError(
                    "retired active tree contains credential-shaped entries; cleanup is halted"
                )
            self._assert_tree_fingerprint(path, expected, "retired active tree")
            _safe_owned_remove(path, parent=self.active_root.parent)
        self._retired_fingerprints.pop(path, None)

    def promote(
        self,
        candidate: str | os.PathLike[str] | CandidateRevision,
        evaluation_receipt: EvaluationReceipt | Mapping[str, Any] | Any,
        *,
        mode: PromotionMode | EvaluationMode | str | None = None,
        authorized: bool | None = None,
        host: Any = None,
        sandbox_attestation: SandboxAttestation | Mapping[str, Any] | None = None,
        attestation: SandboxAttestation | Mapping[str, Any] | None = None,
    ) -> PromotionOutcome:
        """Promote one accepted receipt, or record a safe rejection.

        Configuration/evidence errors raise ``PromotionError``.  A valid
        evaluator receipt that says ``accepted=False`` returns a rejected
        ``PromotionOutcome`` and leaves the active tree untouched.
        """

        with self._lock, _promotion_ledger_file_lock(self._controller_lock_path):
            if self.persistence_path is not None:
                # Re-check the lexical/resolved boundary immediately before
                # any candidate or active-tree operation.  The path may have
                # been replaced by a reparse point after construction.
                self.persistence_path = self._validate_persistence_location(
                    self.persistence_path
                )
            self._refresh_manifest_state()
            if sandbox_attestation is not None and attestation is not None and sandbox_attestation != attestation:
                raise PromotionError("sandbox_attestation and attestation disagree")
            supplied_attestation = (
                sandbox_attestation if sandbox_attestation is not None else attestation
            )
            if mode is None:
                inferred_mode = (
                    evaluation_receipt.get("mode", EVOLUTION)
                    if isinstance(evaluation_receipt, Mapping)
                    else getattr(evaluation_receipt, "mode", EVOLUTION)
                )
            else:
                inferred_mode = mode
            requested_mode = PromotionMode.parse(inferred_mode)
            candidate_path = self._candidate_path(candidate)
            # Re-check names after receipt/mode resolution: the candidate is
            # an external worktree and may have changed since construction.
            if _sensitive_entries(candidate_path):
                raise CandidateIsolationError(
                    "candidate contains credential-shaped paths; private material must stay outside the candidate"
                )
            candidate_revision_id = candidate.revision_id if isinstance(candidate, CandidateRevision) else None
            candidate_fingerprint = directory_fingerprint(
                candidate_path,
                budget=self.resource_budget,
            )
            if isinstance(candidate, CandidateRevision):
                candidate_revision_id = candidate.revision_id
                if candidate.fingerprint != candidate_fingerprint:
                    return self._reject(
                        mode=requested_mode,
                        receipt=None,
                        candidate_revision_id=candidate_revision_id,
                        candidate_fingerprint=candidate_fingerprint,
                        reason="candidate descriptor fingerprint is stale",
                    )
            if _sensitive_entries(self.active_root):
                raise ProtectedFileError(
                    "active_root contains credential-shaped paths; move private material outside the active tree"
                )
            before = directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )
            if before != self._active_fingerprint or self._protected_snapshot() != self._protected_hashes:
                return self._reject(
                    mode=requested_mode,
                    receipt=None,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    reason="active tree or protected paths changed outside controller",
                )
            # A retained backup represents an unconsumed rollback point.  Do
            # not overwrite it with a second promotion; stale controllers
            # constructed before another process committed will have already
            # failed the live-baseline check above and can record a normal
            # rejection for their own ledger.
            if self.rollback_available:
                raise PromotionError(
                    "a previous promotion is awaiting rollback; rollback before another promotion"
                )
            receipt = self._coerce_receipt(evaluation_receipt)
            candidate_revision_id = receipt.candidate_revision_id
            receipt, receipt_reason = self._validate_receipt(
                receipt,
                candidate_fingerprint=candidate_fingerprint,
                requested_mode=requested_mode,
                active_fingerprint=before,
            )
            if receipt_reason is not None:
                # A valid but failed hard gate is evidence of a rejected
                # candidate, never a reason to touch active state.
                return self._reject(
                    mode=requested_mode,
                    receipt=receipt,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    reason=receipt_reason,
                )
            violations = self._candidate_protected_violations(candidate_path)
            if violations:
                return self._reject(
                    mode=requested_mode,
                    receipt=receipt,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    reason="protected paths changed: " + ", ".join(violations),
                )
            if not self._authorised(authorized, host):
                event = self._record(
                    action="authorization_denied",
                    mode=requested_mode,
                    receipt=receipt,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    before=before,
                    after=before,
                    authorized=False,
                    reason="host authorized=True is required for active writes",
                )
                raise AuthorizationError(
                    f"active promotion requires explicit host authorized=True (record {event.event_hash})"
                )

            attestation_obj: SandboxAttestation | None = None
            if self.require_sandbox_attestation:
                try:
                    # Consume only after receipt, candidate, protected paths,
                    # and host authorization have all passed.  A failed
                    # staging/swap still burns this one-time proof, forcing a
                    # fresh host attestation for any retry.
                    attestation_obj = self._validate_sandbox_attestation(
                        supplied_attestation,
                        receipt=receipt,
                        candidate_fingerprint=candidate_fingerprint,
                        mode=requested_mode,
                        consume=True,
                    )
                except SandboxAttestationError as exc:
                    try:
                        event = self._record_attestation_failure(
                            mode=requested_mode,
                            receipt=receipt,
                            candidate_revision_id=candidate_revision_id,
                            candidate_fingerprint=candidate_fingerprint,
                            before=before,
                            reason=str(exc),
                        )
                    except Exception:
                        raise
                    raise SandboxAttestationError(
                        f"sandbox attestation rejected (record {event.event_hash})"
                    ) from exc

            stage: Path | None = None
            backup: Path | None = None
            manifest: PromotionManifest | None = None
            event: PromotionEvent | None = None
            try:
                # Materialize once more into a disposable directory before
                # staging, so a candidate cannot alias active during copy.
                with CandidateSandbox(
                    candidate_path,
                    resource_budget=self.resource_budget,
                ) as sandbox:
                    if sandbox.fingerprint != candidate_fingerprint or not sandbox.verify_source_unchanged():
                        return self._reject(
                            mode=requested_mode,
                            receipt=receipt,
                            candidate_revision_id=candidate_revision_id,
                            candidate_fingerprint=candidate_fingerprint,
                            reason="candidate changed while entering isolated sandbox",
                        )
                    stage = self._stage_candidate(sandbox.path)
                # Reserve the sibling backup name and durably record the
                # transaction before the first rename.  If this process dies
                # after either rename, a fresh controller can reconcile the
                # directory shape instead of guessing from in-memory state.
                backup = self._new_sibling("backup")
                stage_fingerprint = directory_fingerprint(
                    stage,
                    budget=self.resource_budget,
                )
                if _sensitive_entries(stage):
                    raise CandidateIsolationError(
                        "staged candidate contains credential-shaped paths"
                    )
                manifest = PromotionManifest(
                    root_digest=self._root_digest,
                    phase=_MANIFEST_PHASE_PREPARED,
                    operation_id=uuid.uuid4().hex,
                    backup_name=backup.name,
                    stage_name=stage.name,
                    stage_fingerprint=stage_fingerprint,
                    before_fingerprint=before,
                    candidate_fingerprint=candidate_fingerprint,
                    receipt_hash=receipt.receipt_hash,
                    mode=requested_mode.value,
                    updated_at=_utc_now(),
                )
                self._persist_manifest(manifest)
                self._planned_backup_path = backup
                # Re-check live state immediately before the swap to close a
                # concurrent-writer race.
                if (
                    directory_fingerprint(self.active_root, budget=self.resource_budget) != before
                    or self._protected_snapshot() != self._protected_hashes
                ):
                    _safe_owned_remove(stage, parent=self.active_root.parent)
                    stage = None
                    self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    manifest = None
                    backup = None
                    self._planned_backup_path = None
                    return self._reject(
                        mode=requested_mode,
                        receipt=receipt,
                        candidate_revision_id=candidate_revision_id,
                        candidate_fingerprint=candidate_fingerprint,
                        reason="active tree changed during candidate staging",
                    )
                self._active_move_manifest = manifest
                try:
                    swapped_backup = self._swap_in(stage)
                finally:
                    self._active_move_manifest = None
                if backup is None or swapped_backup != backup:
                    raise PromotionError("promotion backup reservation was not honored")
                stage = None
                after = directory_fingerprint(
                    self.active_root,
                    budget=self.resource_budget,
                )
                manifest = manifest.with_phase(
                    _MANIFEST_PHASE_SWAPPED,
                    stage_name=None,
                    stage_fingerprint=None,
                    after_fingerprint=after,
                )
                self._persist_manifest(manifest)
                if self._protected_snapshot() != self._protected_hashes:
                    self._restore_backup(backup, expected_fingerprint=before)
                    backup = None
                    self._active_fingerprint = before
                    self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    manifest = None
                    raise ProtectedFileError("protected paths changed during promotion")
                # Persist the promotion receipt only after the new tree has
                # passed its post-swap checks.  If persistence fails, restore
                # the retained old tree before surfacing the error; otherwise
                # an active mutation would exist without an append-only
                # receipt proving it.
                try:
                    event = self._record(
                        action="promoted",
                        mode=requested_mode,
                        receipt=receipt,
                        candidate_revision_id=candidate_revision_id,
                        candidate_fingerprint=candidate_fingerprint,
                        before=before,
                        after=after,
                        authorized=True,
                        reason="accepted fixed evaluation receipt",
                        attestation=attestation_obj,
                        metadata={"promotion_operation_id": manifest.operation_id},
                    )
                except Exception as ledger_exc:
                    try:
                        self._restore_backup(backup, expected_fingerprint=before)
                    except Exception as restore_exc:
                        raise PromotionError(
                            "promotion receipt failed and active restoration failed"
                        ) from restore_exc
                    backup = None
                    self._active_fingerprint = before
                    self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    manifest = None
                    raise PromotionError(
                        "promotion receipt could not be appended; active tree was restored"
                    ) from ledger_exc
                # The append is the durable proof.  Persist COMMITTED after
                # it; if this final manifest write fails, leave the SWAPPED
                # record and the backup in place so restart can match the
                # ledger event and converge safely.
                committed_manifest = manifest.with_phase(
                    _MANIFEST_PHASE_COMMITTED,
                    event_hash=event.event_hash,
                )
                self._backup_path = backup
                self._active_fingerprint = after
                try:
                    self._persist_manifest(committed_manifest)
                except Exception as manifest_exc:
                    raise PromotionError(
                        "promotion receipt is durable but transaction manifest could not be committed"
                    ) from manifest_exc
                return self._outcome(
                    accepted=True,
                    action="promoted",
                    mode=requested_mode,
                    receipt=receipt,
                    candidate_revision_id=candidate_revision_id,
                    candidate_fingerprint=candidate_fingerprint,
                    before=before,
                    after=after,
                    rollback_available=True,
                    event=event,
                    reason="promoted after fixed evaluator gates",
                    attestation=attestation_obj,
                )
            except (ProtectedFileError, CandidateIsolationError, PromotionError):
                if stage is not None:
                    _safe_owned_remove(stage, parent=self.active_root.parent)
                if event is None and backup is not None and backup.exists():
                    try:
                        self._restore_backup(backup, expected_fingerprint=before)
                        self._active_fingerprint = before
                    except Exception as restore_exc:
                        raise PromotionError(
                            "promotion failed and active restoration failed"
                        ) from restore_exc
                if event is None and manifest is not None and (backup is None or not backup.exists()):
                    try:
                        self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    except Exception:
                        # Keep the non-idle manifest as recovery evidence when
                        # the host cannot persist the cleanup marker.
                        pass
                self._planned_backup_path = None
                raise
            except OSError as exc:
                if stage is not None:
                    _safe_owned_remove(stage, parent=self.active_root.parent)
                if event is None and backup is not None and backup.exists():
                    try:
                        self._restore_backup(backup, expected_fingerprint=before)
                        self._active_fingerprint = before
                    except Exception as restore_exc:
                        raise PromotionError(
                            "promotion failed and active restoration failed"
                        ) from restore_exc
                if event is None and manifest is not None and (backup is None or not backup.exists()):
                    try:
                        self._persist_manifest(PromotionManifest.idle(self._root_digest))
                    except Exception:
                        pass
                self._planned_backup_path = None
                raise PromotionError("atomic promotion failed") from exc

    # Readable aliases used by orchestrators.
    promote_candidate = promote
    apply = promote
    commit = promote

    def rollback(
        self,
        *,
        authorized: bool | None = None,
        host: Any = None,
        reason: str = "explicit host rollback",
    ) -> PromotionOutcome:
        """Restore the retained previous active tree after an authorized call."""

        with self._lock, _promotion_ledger_file_lock(self._controller_lock_path):
            self._refresh_manifest_state()
            if not self.rollback_available:
                raise PromotionError("no retained active revision is available for rollback")
            if not self._authorised(authorized, host):
                event = self._record(
                    action="authorization_denied",
                    mode=PromotionMode.RECOVERY,
                    receipt=None,
                    candidate_revision_id=None,
                    candidate_fingerprint=None,
                    before=self._active_fingerprint,
                    after=self._active_fingerprint,
                    authorized=False,
                    reason="host authorized=True is required for active rollback",
                )
                raise AuthorizationError(
                    f"active rollback requires explicit host authorized=True (record {event.event_hash})"
                )
            assert self._backup_path is not None
            backup = self._backup_path
            before = directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )
            if self._protected_snapshot() != self._protected_hashes:
                raise ProtectedFileError("cannot rollback while protected paths have drifted")
            current = self._new_sibling("rollback-current")
            prior_manifest = _read_promotion_manifest(self._manifest_path)
            if prior_manifest is not None and prior_manifest.phase != _MANIFEST_PHASE_COMMITTED:
                raise PromotionError("rollback requires a committed promotion manifest")
            if prior_manifest is not None and self._matching_promoted_event(prior_manifest) is None:
                raise PromotionError("rollback requires the matching durable promotion receipt")
            rollback_target = directory_fingerprint(
                backup,
                budget=self.resource_budget,
            )
            if prior_manifest is not None:
                if prior_manifest.before_fingerprint != rollback_target:
                    raise PromotionError("rollback backup fingerprint does not match committed baseline")
                if prior_manifest.after_fingerprint != before:
                    raise PromotionError("active tree changed since promotion; rollback is blocked")
            elif before != self._active_fingerprint:
                raise PromotionError("active tree changed outside controller; rollback is blocked")
            rollback_manifest = PromotionManifest(
                root_digest=self._root_digest,
                phase=_MANIFEST_PHASE_ROLLBACK_STARTED,
                operation_id=(
                    prior_manifest.operation_id
                    if prior_manifest is not None
                    else uuid.uuid4().hex
                ),
                backup_name=backup.name,
                current_name=current.name,
                before_fingerprint=before,
                candidate_fingerprint=(
                    prior_manifest.candidate_fingerprint if prior_manifest is not None else before
                ),
                after_fingerprint=rollback_target,
                receipt_hash=prior_manifest.receipt_hash if prior_manifest is not None else None,
                event_hash=prior_manifest.event_hash if prior_manifest is not None else None,
                mode=PromotionMode.RECOVERY.value,
                updated_at=_utc_now(),
            )
            self._persist_manifest(rollback_manifest)
            event: PromotionEvent | None = None
            try:
                os.replace(self.active_root, current)
                _fsync_directory(self.active_root.parent)
                os.replace(backup, self.active_root)
                _fsync_directory(self.active_root.parent)
                after = directory_fingerprint(
                    self.active_root,
                    budget=self.resource_budget,
                )
                if self._protected_snapshot() != self._protected_hashes:
                    raise ProtectedFileError("rollback would alter protected paths")
                event = self._record(
                    action="rolled_back",
                    mode=PromotionMode.RECOVERY,
                    receipt=None,
                    candidate_revision_id=None,
                    candidate_fingerprint=None,
                    before=before,
                    after=after,
                    authorized=True,
                    reason=reason,
                    metadata={"promotion_operation_id": rollback_manifest.operation_id},
                )
            except Exception as exc:
                # Try to restore the pre-rollback active tree if the swap or
                # verification failed.
                try:
                    if not self.active_root.exists() and current.exists():
                        # First rename completed, second did not.
                        os.replace(current, self.active_root)
                        _fsync_directory(self.active_root.parent)
                    elif self.active_root.exists() and not backup.exists() and current.exists():
                        # Both renames completed, but the operation did not
                        # obtain a durable rollback receipt.  Restore the
                        # candidate + backup pair so no state is lost.
                        os.replace(self.active_root, backup)
                        _fsync_directory(self.active_root.parent)
                        os.replace(current, self.active_root)
                        _fsync_directory(self.active_root.parent)
                except Exception:
                    pass
                if event is None:
                    # Reconstruct the original committed manifest when the
                    # active tree has been restored.  If the host cannot write
                    # it, leave the in-flight record for fail-closed recovery.
                    try:
                        if backup.exists() and self.active_root.exists():
                            original = (
                                prior_manifest
                                if prior_manifest is not None
                                else PromotionManifest(
                                    root_digest=self._root_digest,
                                    phase=_MANIFEST_PHASE_COMMITTED,
                                    operation_id=rollback_manifest.operation_id,
                                    backup_name=backup.name,
                                    before_fingerprint=rollback_target,
                                    candidate_fingerprint=rollback_manifest.candidate_fingerprint,
                                    after_fingerprint=before,
                                    receipt_hash=rollback_manifest.receipt_hash,
                                    event_hash=rollback_manifest.event_hash,
                                    mode=EVOLUTION,
                                    updated_at=_utc_now(),
                                )
                            )
                            self._persist_manifest(original)
                    except Exception:
                        pass
                if isinstance(exc, PromotionError):
                    raise
                raise PromotionError("rollback failed") from exc
            # The rollback event is durable before we consume the retired
            # candidate.  A crash after this point is reconciled from the
            # event plus the ROLLBACK_STARTED manifest.
            completed_manifest = rollback_manifest.with_phase(
                _MANIFEST_PHASE_ROLLED_BACK,
                backup_name=None,
                event_hash=event.event_hash if event is not None else None,
            )
            self._persist_manifest(completed_manifest)
            self._remove_manifest_owned(
                current,
                expected_fingerprint=before,
                label="rollback current tree",
            )
            self._persist_manifest(PromotionManifest.idle(self._root_digest))
            self._backup_path = None
            self._active_fingerprint = after
            return self._outcome(
                accepted=True,
                action="rolled_back",
                mode=PromotionMode.RECOVERY,
                receipt=None,
                candidate_revision_id=None,
                candidate_fingerprint=None,
                before=before,
                after=after,
                rollback_available=False,
                event=event,
                reason=reason,
            )

    restore = rollback

    def discard_rollback(
        self,
        *,
        authorized: bool | None = None,
        host: Any = None,
        reason: str = "explicit host discard of rollback point",
    ) -> PromotionOutcome:
        """Explicitly and irreversibly discard a retained rollback point.

        ``close()`` is intentionally non-destructive.  Callers that have
        independently decided the previous revision is no longer needed must
        use this method with an explicit authorization; the two-phase
        FINALIZING manifest makes a crash visible rather than silently
        converting cleanup into an unrecorded state change.
        """

        with self._lock, _promotion_ledger_file_lock(self._controller_lock_path):
            self._refresh_manifest_state()
            if not self.rollback_available:
                raise PromotionError("no retained active revision is available to discard")
            if not self._authorised(authorized, host):
                event = self._record(
                    action="authorization_denied",
                    mode=PromotionMode.RECOVERY,
                    receipt=None,
                    candidate_revision_id=None,
                    candidate_fingerprint=None,
                    before=self._active_fingerprint,
                    after=self._active_fingerprint,
                    authorized=False,
                    reason="host authorized=True is required to discard a rollback point",
                )
                raise AuthorizationError(
                    f"discarding a rollback point requires explicit host authorized=True (record {event.event_hash})"
                )
            assert self._backup_path is not None
            backup = self._backup_path
            manifest = _read_promotion_manifest(self._manifest_path)
            if manifest is None or manifest.phase not in {
                _MANIFEST_PHASE_COMMITTED,
                _MANIFEST_PHASE_FINALIZING,
            }:
                raise PromotionError("rollback discard requires a committed/finalizing promotion manifest")
            if manifest.backup_name != backup.name:
                raise PromotionError("rollback backup does not match its manifest")
            if manifest.phase == _MANIFEST_PHASE_COMMITTED and self._matching_promoted_event(manifest) is None:
                raise PromotionError("rollback discard requires the matching durable promotion receipt")
            before = directory_fingerprint(
                self.active_root,
                budget=self.resource_budget,
            )
            if manifest.after_fingerprint is not None and before != manifest.after_fingerprint:
                raise PromotionError("cannot discard rollback after active tree drift")
            if manifest.before_fingerprint is not None:
                self._assert_tree_fingerprint(
                    backup, manifest.before_fingerprint, "rollback discard backup"
                )
            if self._protected_snapshot() != self._protected_hashes:
                raise ProtectedFileError("cannot discard rollback while protected paths have drifted")
            finalizing = manifest.with_phase(
                _MANIFEST_PHASE_FINALIZING,
                event_hash=None,
            )
            started = self._matching_discard_started_event(manifest)
            if started is None:
                self._persist_manifest(finalizing)
                started = self._record(
                    action="rollback_discard_started",
                    mode=PromotionMode.RECOVERY,
                    receipt=None,
                    candidate_revision_id=None,
                    candidate_fingerprint=None,
                    before=before,
                    after=before,
                    authorized=True,
                    reason=reason,
                    metadata={"promotion_operation_id": manifest.operation_id},
                )
            # Only remove the exact manifest-owned sibling.  If this raises,
            # FINALIZING remains on disk and a new controller will refuse to
            # guess whether cleanup completed.
            self._remove_manifest_owned(
                backup,
                expected_fingerprint=manifest.before_fingerprint,
                label="rollback discard backup",
            )
            _fsync_directory(self.active_root.parent)
            discarded = self._record(
                action="rollback_discarded",
                mode=PromotionMode.RECOVERY,
                receipt=None,
                candidate_revision_id=None,
                candidate_fingerprint=None,
                before=before,
                after=before,
                authorized=True,
                reason=reason,
                metadata={
                    "promotion_operation_id": manifest.operation_id,
                    "discard_started_event": started.event_hash,
                },
            )
            self._persist_manifest(
                finalizing.with_phase(
                    _MANIFEST_PHASE_FINALIZING,
                    backup_name=None,
                    event_hash=discarded.event_hash,
                )
            )
            self._persist_manifest(PromotionManifest.idle(self._root_digest))
            self._backup_path = None
            self._active_fingerprint = before
            return self._outcome(
                accepted=True,
                action="rollback_discarded",
                mode=PromotionMode.RECOVERY,
                receipt=None,
                candidate_revision_id=None,
                candidate_fingerprint=None,
                before=before,
                after=before,
                rollback_available=False,
                event=discarded,
                reason=reason,
            )

    # Naming aliases for host orchestration code.
    finalize_backup = discard_rollback
    discard_backup = discard_rollback

    def verify(self) -> bool:
        """Verify active protected anchors, fingerprint tracking, and ledger."""

        with self._lock, _promotion_ledger_file_lock(self._controller_lock_path):
            try:
                return (
                    directory_fingerprint(
                        self.active_root,
                        budget=self.resource_budget,
                    )
                    == self._active_fingerprint
                    and self._protected_snapshot() == self._protected_hashes
                    and self.ledger.verify()
                    and self._verify_manifest_state()
                )
            except Exception:
                return False

    def close(self) -> None:
        with self._lock, _promotion_ledger_file_lock(self._controller_lock_path):
            # Closing a controller is not authorization to destroy the only
            # rollback proof.  The durable COMMITTED manifest and backup stay
            # available to a later controller; use discard_rollback() for an
            # explicit irreversible cleanup.
            for path in list(self._retired_paths):
                self._remove_retired_owned(path)
            self._retired_paths.clear()

    def __enter__(self) -> "PromotionController":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "EVOLUTION",
    "RECOVERY",
    "EVOLUTION_MODE",
    "RECOVERY_MODE",
    "PROMOTION_SCHEMA_VERSION",
    "ATTESTATION_SCHEMA_VERSION",
    "PROMOTION_MANIFEST_SCHEMA_VERSION",
    "PROMOTION_MANIFEST_MAX_BYTES",
    "PROMOTION_MANIFEST_PHASE_IDLE",
    "PROMOTION_MANIFEST_PHASE_PREPARED",
    "PROMOTION_MANIFEST_PHASE_ACTIVE_MOVED",
    "PROMOTION_MANIFEST_PHASE_SWAPPED",
    "PROMOTION_MANIFEST_PHASE_COMMITTED",
    "PROMOTION_MANIFEST_PHASE_ROLLBACK_STARTED",
    "PROMOTION_MANIFEST_PHASE_ROLLED_BACK",
    "PROMOTION_MANIFEST_PHASE_FINALIZING",
    "PROMOTION_MANIFEST_PHASES",
    "SANDBOX_ATTESTATION_MAX_TTL_SEC",
    "REQUIRED_SANDBOX_CAPABILITIES",
    "EvolutionError",
    "PromotionError",
    "ReceiptValidationError",
    "AuthorizationError",
    "PromotionAuthorizationError",
    "ProtectedFileError",
    "ProtectedPathError",
    "CandidateIsolationError",
    "LedgerError",
    "EvaluationReceiptError",
    "SandboxAttestationError",
    "HostSandboxAttestationError",
    "PromotionMode",
    "EvolutionMode",
    "PromotionType",
    "CandidateSandbox",
    "PromotionOutcome",
    "PromotionEvent",
    "PromotionManifest",
    "PromotionReceipt",
    "HashReceipt",
    "PromotionLedger",
    "PromotionController",
    "SandboxAttestation",
    "HostSandboxAttestation",
    "SandboxExecutorAttestation",
    "SandboxAttestor",
    "HostSandboxAttestor",
]
