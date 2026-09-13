"""Independent, local-only evaluation harness for candidate revisions.

The life kernel owns identity and lifecycle.  This module owns a deliberately
separate seam for evaluating a *candidate* implementation before it can be
promoted by a caller.  It is intentionally standard-library only and has no
network, GitHub, credential, or production-workspace integration.

The contract is conservative:

* a candidate is copied to a fresh temporary directory and executed there;
* fixture files are copied to a sibling directory and are never handed out
  from the evaluator's source tree;
* the evaluator and fixture hashes are pinned before execution and checked
  afterwards;
* the child process receives a scrubbed environment and no stdin;
* a fixture-owned judge must emit an explicit, verified JSON result (a
  candidate cannot judge or promote itself);
* evolution requires strict improvement over a trusted baseline, while
  recovery only requires restoring its baseline metrics;
* no candidate is ever copied back to a live directory.  Rejected candidates
  therefore have a concrete rollback receipt by construction.

This is an operational safety boundary, not a kernel-level sandbox.  A host
that needs OS-level denial of networking or writes outside the temporary tree
must run this harness inside a separate container/job policy.  The harness
still fails closed when it detects fixture/evaluator mutation or a sensitive
environment leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import uuid


HARNESS_SCHEMA_VERSION = 1
HARNESS_VERSION = "0.1.0"
RESULT_PREFIX = "EVALUATION_RESULT:"
EMPTY_DIRECTORY_HASH = hashlib.sha256(b"brain-memory:empty-directory:v1").hexdigest()
# An absent fixture set is represented by the same canonical empty-directory
# digest as an explicitly empty fixture directory.
EMPTY_FIXTURE_HASH = EMPTY_DIRECTORY_HASH


class EvaluationHarnessError(RuntimeError):
    """Base error for invalid harness configuration or evidence."""


class HarnessConfigurationError(EvaluationHarnessError):
    """The evaluator itself was configured with an unsafe or invalid input."""


class CandidateIsolationError(EvaluationHarnessError):
    """A candidate or fixture cannot be safely materialized in isolation."""


class CandidateResultError(EvaluationHarnessError):
    """A candidate did not produce the required result protocol."""


class EvaluationMode(str, Enum):
    EVOLUTION = "evolution"
    RECOVERY = "recovery"

    @classmethod
    def parse(cls, value: "EvaluationMode | str") -> "EvaluationMode":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        aliases = {"repair": cls.RECOVERY, "restore": cls.RECOVERY, "evolve": cls.EVOLUTION}
        if text in aliases:
            return aliases[text]
        try:
            return cls(text)
        except ValueError as exc:
            raise HarnessConfigurationError(f"unknown evaluation mode: {value!r}") from exc


class HardGate(str, Enum):
    """Evaluator-owned gate vocabulary; candidates cannot add/remove gates."""

    STARTUP = "startup"
    INTEGRITY = "integrity"
    RESOURCE_BUDGET = "resource_budget"
    PERMISSION_BOUNDARY = "permission_boundary"
    INDEPENDENT_JUDGE = "independent_judge"
    BASELINE_CONTRACT = "baseline_contract"
    VERIFICATION = "verification"
    IMPROVEMENT = "improvement"
    RECOVERY_BASELINE = "recovery_baseline"
    ROLLBACK = "rollback"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch if ord(ch) >= 32 or ch in "\r\n\t" else " " for ch in text)
    return text[:limit]


def _is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points without traversal."""

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
        attributes = getattr(stat_result, "st_file_attributes", None)
        if attributes is None:
            return True
        return bool(int(attributes) & 0x400)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


def _lexists(path: Path) -> bool:
    """Return true for ordinary entries and dangling links alike."""

    try:
        return bool(os.path.lexists(str(path)))
    except (OSError, ValueError, TypeError):
        return True


def _assert_no_reparse_components(value: str | os.PathLike[str]) -> None:
    """Inspect lexical path components before canonical resolution."""

    raw_path = Path(value)
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    try:
        parts = raw_path.parts
        if not parts:
            return
        current = Path(parts[0])
        if _is_link_like(current):
            raise HarnessConfigurationError("reparse-point path components are outside the local contract")
        for part in parts[1:]:
            current = current / part
            if _is_link_like(current):
                raise HarnessConfigurationError("reparse-point path components are outside the local contract")
            if not current.exists():
                break
    except HarnessConfigurationError:
        raise
    except (OSError, ValueError) as exc:
        raise HarnessConfigurationError("cannot inspect local path components") from exc


def _resolve_local_path(value: str | os.PathLike[str], *, strict: bool = True) -> Path:
    """Resolve a local path without touching URL/UNC network namespaces."""

    raw = os.fspath(value)
    text = os.fsdecode(raw)
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE) or text.startswith(("\\\\", "//")):
        raise HarnessConfigurationError("network/URL paths are outside the local evaluation contract")
    _assert_no_reparse_components(value)
    return Path(value).resolve(strict=strict)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _freeze(value: Any, depth: int = 0) -> Any:
    """Bound and freeze evidence so a receipt cannot be changed in place."""

    if depth > 5:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, str)):
        return _bounded_text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        items = list(value.items())[:64]
        return MappingProxyType({_bounded_text(k, 120): _freeze(v, depth + 1) for k, v in items})
    if isinstance(value, (list, tuple, set)):
        sequence = sorted(value, key=str) if isinstance(value, set) else list(value)
        return tuple(_freeze(item, depth + 1) for item in sequence[:64])
    return _bounded_text(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


_SENSITIVE_RE = re.compile(
    r"(^|[._-])(env|key|token|secret|password|passwd|credential|cookie|auth)([._-]|$)",
    re.IGNORECASE,
)
_SENSITIVE_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH|GITHUB|DEEPSEEK|DASHSCOPE|GLM|OPENAI|AWS|AZURE)",
    re.IGNORECASE,
)

_SENSITIVE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__secrets__",
    "secrets",
    "credentials",
}


def _is_sensitive_name(name: str) -> bool:
    base = Path(name).name
    if base.lower() in {".env.example", ".env.sample", ".env.template"}:
        return False
    # .env and common private-key files are handled without opening them.
    if base.lower() in {
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
    return bool(_SENSITIVE_RE.search(base) or re.search(r"api[_-]?key", base, re.IGNORECASE))


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Finite process and artifact limits enforced by the harness."""

    timeout_sec: float = 10.0
    max_stdout_bytes: int = 64 * 1024
    max_stderr_bytes: int = 64 * 1024
    max_output_bytes: int = 128 * 1024
    max_files: int = 2_048
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        timeout = float(self.timeout_sec)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 24 * 60 * 60:
            raise HarnessConfigurationError("timeout_sec must be finite and in (0, 86400]")
        object.__setattr__(self, "timeout_sec", timeout)
        for name in (
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_output_bytes",
            "max_files",
            "max_file_bytes",
            "max_total_bytes",
        ):
            value = int(getattr(self, name))
            if value < 1:
                raise HarnessConfigurationError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        # ``max_output_bytes`` is an aggregate cap.  It may intentionally be
        # smaller than either per-stream cap (for example, a smoke test that
        # permits only a tiny combined response), so the three limits are
        # checked independently at run time.

    @property
    def max_runtime_sec(self) -> float:
        """Readable alias for callers that use runtime terminology."""

        return self.timeout_sec

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_sec": self.timeout_sec,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


@dataclass(frozen=True, slots=True)
class FixtureEntry:
    relative_path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }


def _iter_regular_files(
    root: Path,
    *,
    budget: "ResourceBudget | None" = None,
) -> Iterable[tuple[Path, str]]:
    """Yield safe regular files in deterministic order.

    Symlinks are rejected rather than followed.  Sensitive files are skipped
    by name without opening them; this prevents accidental credential reads
    when a caller points the harness at a normal development checkout.
    """

    root = _resolve_local_path(root, strict=True)
    if _is_link_like(root) or not root.is_dir():
        raise CandidateIsolationError(f"expected directory: {root}")
    rows: list[tuple[Path, str]] = []
    file_count = 0
    total_bytes = 0
    inspected_entries = 0
    # Empty-directory explosions can be just as disruptive as a large file.
    # Keep the metadata walk bounded even before a candidate is copied.
    max_entries = (
        max(1024, int(budget.max_files) * 8)
        if budget is not None
        else 100_000
    )
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for raw_dir in dirs:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise CandidateIsolationError("candidate entry count exceeds the evaluation budget")
            path = current_path / raw_dir
            if _is_link_like(path):
                raise CandidateIsolationError(f"symlink directory is not allowed: {path}")
            if raw_dir.lower() in _SENSITIVE_DIR_NAMES:
                # Never traverse VCS metadata, virtual environments, or
                # obvious secret stores.  This also avoids reading a remote
                # URL or credential accidentally from .git/config.
                continue
            safe_dirs.append(raw_dir)
        dirs[:] = safe_dirs
        for raw_file in files:
            inspected_entries += 1
            if inspected_entries > max_entries:
                raise CandidateIsolationError("candidate entry count exceeds the evaluation budget")
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
                raise CandidateIsolationError(f"cannot stat candidate file: {path}") from exc
            if not stat.S_ISREG(mode):
                raise CandidateIsolationError(f"non-regular file is not allowed: {path}")
            # Do not read through a hard link that aliases a host-owned file
            # outside the candidate/fixture tree.  Symlink checks alone do
            # not cover this case because hard links look like regular files.
            if getattr(stat_result, "st_nlink", 1) > 1:
                raise CandidateIsolationError(
                    f"hard-linked candidate file is not allowed: {path}"
                )
            size = max(0, int(stat_result.st_size))
            if budget is not None:
                if file_count >= budget.max_files:
                    raise CandidateIsolationError("candidate file count exceeds the evaluation budget")
                if size > budget.max_file_bytes:
                    raise CandidateIsolationError(
                        f"candidate file exceeds max_file_bytes: {path}"
                    )
                if total_bytes + size > budget.max_total_bytes:
                    raise CandidateIsolationError("candidate bytes exceed the evaluation budget")
            file_count += 1
            total_bytes += size
            rows.append((path, rel))
    rows.sort(key=lambda item: item[1])
    return rows


def _manifest_for(
    root: Path,
    *,
    budget: "ResourceBudget | None" = None,
) -> tuple[FixtureEntry, ...]:
    entries: list[FixtureEntry] = []
    total_bytes = 0
    for path, rel in _iter_regular_files(root, budget=budget):
        try:
            stat_result = path.stat()
            if budget is not None and int(stat_result.st_size) > budget.max_file_bytes:
                raise CandidateIsolationError(
                    f"file exceeds max_file_bytes while hashing: {path}"
                )
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if budget is not None and size > budget.max_file_bytes:
                        raise CandidateIsolationError(
                            f"file exceeds max_file_bytes while hashing: {path}"
                        )
                    hasher.update(chunk)
            if budget is not None and total_bytes + size > budget.max_total_bytes:
                raise CandidateIsolationError("directory bytes exceed the evaluation budget")
        except OSError as exc:
            raise CandidateIsolationError(f"cannot read fixture file: {path}") from exc
        entries.append(
            FixtureEntry(relative_path=rel, size=size, sha256=hasher.hexdigest())
        )
        total_bytes += size
    return tuple(entries)


def _digest_manifest(entries: Iterable[FixtureEntry], *, empty_marker: str = "") -> str:
    hasher = hashlib.sha256()
    material = list(entries)
    if not material and empty_marker:
        hasher.update(empty_marker.encode("utf-8"))
    for entry in sorted(material, key=lambda item: item.relative_path):
        hasher.update(entry.relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(entry.size).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(entry.sha256.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def directory_fingerprint(
    path: str | os.PathLike[str],
    *,
    budget: "ResourceBudget | None" = None,
) -> str:
    """Return a deterministic SHA-256 fingerprint for a local directory."""

    root = _resolve_local_path(path, strict=True)
    if not root.is_dir():
        raise CandidateIsolationError(f"expected directory: {root}")
    return _digest_manifest(
        _manifest_for(root, budget=budget),
        empty_marker="brain-memory:empty-directory:v1",
    )


@dataclass(frozen=True, slots=True)
class FixtureSet:
    """Versioned fixture manifest materialized into each evaluation sandbox."""

    source: Path | None
    entries: tuple[FixtureEntry, ...]
    digest: str

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str] | None,
        *,
        budget: "ResourceBudget | None" = None,
    ) -> "FixtureSet":
        if path is None:
            return cls(source=None, entries=(), digest=EMPTY_FIXTURE_HASH)
        source = _resolve_local_path(path, strict=True)
        if not source.is_dir():
            raise HarnessConfigurationError(f"fixture root must be a directory: {source}")
        entries = _manifest_for(source, budget=budget or ResourceBudget())
        digest = _digest_manifest(entries, empty_marker="brain-memory:empty-directory:v1")
        return cls(source=source, entries=entries, digest=digest)

    @property
    def fixture_hash(self) -> str:
        return self.digest

    @property
    def hash(self) -> str:
        """Short alias used by receipt/orchestration adapters."""

        return self.digest

    @property
    def sha256(self) -> str:
        return self.digest

    def materialize(
        self,
        destination: Path,
        *,
        budget: "ResourceBudget | None" = None,
    ) -> Path:
        """Copy fixtures into ``destination`` and make them read-only best effort."""

        destination.mkdir(parents=True, exist_ok=False)
        selected_budget = budget or ResourceBudget()
        copied_files = 0
        copied_bytes = 0
        if self.source is not None:
            for entry in self.entries:
                if copied_files >= selected_budget.max_files:
                    raise CandidateIsolationError("fixture file count exceeds the evaluation budget")
                if entry.size > selected_budget.max_file_bytes:
                    raise CandidateIsolationError(
                        f"fixture file exceeds max_file_bytes: {entry.relative_path}"
                    )
                if copied_bytes + entry.size > selected_budget.max_total_bytes:
                    raise CandidateIsolationError("fixture bytes exceed the evaluation budget")
                source_file = self.source / Path(entry.relative_path)
                target_file = destination / Path(entry.relative_path)
                target_file.parent.mkdir(parents=True, exist_ok=True)
                copied = _copy_regular_file(
                    source_file,
                    target_file,
                    max_bytes=selected_budget.max_file_bytes,
                )
                copied_files += 1
                copied_bytes += copied
                if copied_bytes > selected_budget.max_total_bytes:
                    raise CandidateIsolationError("fixture bytes exceed the evaluation budget")
        _make_tree_read_only(destination)
        return destination

    def verify(
        self,
        materialized: Path,
        *,
        budget: "ResourceBudget | None" = None,
    ) -> bool:
        try:
            entries = _manifest_for(materialized, budget=budget or ResourceBudget())
        except (OSError, EvaluationHarnessError):
            return False
        actual = _digest_manifest(entries, empty_marker="brain-memory:empty-directory:v1")
        return actual == self.digest


@dataclass(frozen=True, slots=True)
class CandidateRevision:
    """A candidate source descriptor; source contents remain outside the live runtime."""

    revision_id: str
    fingerprint: str
    source_path: str

    def __post_init__(self) -> None:
        revision_id = _bounded_text(self.revision_id, 160).strip()
        fingerprint = _bounded_text(self.fingerprint, 128).strip().lower()
        source_path = _bounded_text(self.source_path, 1000).strip()
        if not revision_id or not fingerprint or not source_path:
            raise HarnessConfigurationError(
                "candidate revision_id, fingerprint, and source_path are required"
            )
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "source_path", source_path)

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        revision_id: str | None = None,
        *,
        budget: "ResourceBudget | None" = None,
    ) -> "CandidateRevision":
        source = _resolve_local_path(path, strict=True)
        # Descriptor creation is often the first operation on a candidate.
        # Apply a finite default here so a caller cannot hash an unbounded
        # self-generated tree before the harness has a chance to enforce its
        # declared budget.
        fingerprint = directory_fingerprint(
            source,
            budget=budget if budget is not None else ResourceBudget(),
        )
        rid = _bounded_text(revision_id, 160).strip() or f"candidate-{fingerprint[:16]}"
        return cls(rid, fingerprint, str(source))

    @property
    def sha256(self) -> str:
        return self.fingerprint


@dataclass(frozen=True, slots=True)
class BaselineRevision:
    """Trusted baseline metadata used for relative progress checks."""

    revision_id: str
    fingerprint: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    source_path: str | None = None
    harness_version: str | None = None
    fixture_hash: str | None = None
    evaluator_hash: str | None = None

    def __post_init__(self) -> None:
        revision_id = _bounded_text(self.revision_id, 160).strip()
        fingerprint = _bounded_text(self.fingerprint, 128).strip().lower()
        if not revision_id or not fingerprint:
            raise HarnessConfigurationError("baseline revision_id and fingerprint are required")
        normalized: dict[str, float] = {}
        if not isinstance(self.metrics, Mapping):
            raise HarnessConfigurationError("baseline metrics must be a mapping")
        for raw_key, raw_value in list(self.metrics.items())[:64]:
            key = _bounded_text(raw_key, 120).strip()
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                raise HarnessConfigurationError(f"invalid baseline metric: {raw_key!r}") from None
            if not key or not math.isfinite(value):
                raise HarnessConfigurationError(f"invalid baseline metric: {raw_key!r}")
            normalized[key] = value
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "metrics", MappingProxyType(normalized))
        if self.source_path is not None:
            object.__setattr__(self, "source_path", str(_resolve_local_path(self.source_path, strict=False)))
        for name, limit in (
            ("harness_version", 80),
            ("fixture_hash", 128),
            ("evaluator_hash", 128),
        ):
            value = _bounded_text(getattr(self, name), limit).strip() or None
            object.__setattr__(self, name, value)

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        metrics: Mapping[str, float] | None = None,
        revision_id: str | None = None,
        *,
        harness_version: str | None = None,
        fixture_hash: str | None = None,
        evaluator_hash: str | None = None,
        budget: "ResourceBudget | None" = None,
    ) -> "BaselineRevision":
        source = _resolve_local_path(path, strict=True)
        fingerprint = directory_fingerprint(
            source,
            budget=budget if budget is not None else ResourceBudget(),
        )
        rid = _bounded_text(revision_id, 160).strip() or f"baseline-{fingerprint[:16]}"
        return cls(
            rid,
            fingerprint,
            metrics or {},
            str(source),
            harness_version,
            fixture_hash,
            evaluator_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "fingerprint": self.fingerprint,
            "metrics": _thaw(self.metrics),
            "source_path": self.source_path,
            "harness_version": self.harness_version,
            "fixture_hash": self.fixture_hash,
            "evaluator_hash": self.evaluator_hash,
        }

    @property
    def sha256(self) -> str:
        return self.fingerprint


@dataclass(frozen=True, slots=True)
class GateResult:
    """One deterministic hard-gate result in an evaluation receipt."""

    name: str
    passed: bool
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = _bounded_text(self.name, 100).strip()
        if not name:
            raise HarnessConfigurationError("gate name must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "detail", _bounded_text(self.detail, 500))
        object.__setattr__(self, "evidence", _freeze(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": _thaw(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReceipt:
    """Immutable, hash-addressed evidence for one candidate run."""

    receipt_id: str
    harness_version: str
    fixture_hash: str
    evaluator_hash: str
    mode: str
    candidate_revision_id: str
    candidate_fingerprint: str
    baseline_revision_id: str | None
    baseline_fingerprint: str | None
    baseline_metrics: Mapping[str, float]
    candidate_metrics: Mapping[str, float]
    primary_dimension: str | None
    improvement: float | None
    gates: tuple[GateResult, ...]
    accepted: bool
    outcome: str
    rollback_performed: bool
    rollback_target: str | None
    isolated: bool
    started_at: str
    finished_at: str
    duration_sec: float
    exit_code: int | None
    timed_out: bool
    stdout_bytes: int
    stderr_bytes: int
    result_verified: bool
    error: str = ""
    receipt_hash: str = ""
    # Small evaluator-owned metadata envelope.  It is intentionally optional
    # for legacy callers, but lets an external executor bind its proof to the
    # immutable receipt without smuggling paths or raw output into metrics.
    # Keep this after ``receipt_hash`` so legacy positional construction keeps
    # its original argument order.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness_version", _bounded_text(self.harness_version, 80))
        object.__setattr__(self, "fixture_hash", _bounded_text(self.fixture_hash, 128))
        object.__setattr__(self, "evaluator_hash", _bounded_text(self.evaluator_hash, 128))
        object.__setattr__(self, "mode", EvaluationMode.parse(self.mode).value)
        object.__setattr__(self, "candidate_revision_id", _bounded_text(self.candidate_revision_id, 160))
        object.__setattr__(self, "candidate_fingerprint", _bounded_text(self.candidate_fingerprint, 128))
        object.__setattr__(self, "baseline_revision_id", _bounded_text(self.baseline_revision_id, 160) or None)
        object.__setattr__(self, "baseline_fingerprint", _bounded_text(self.baseline_fingerprint, 128) or None)
        object.__setattr__(self, "baseline_metrics", _numeric_metrics(self.baseline_metrics))
        object.__setattr__(self, "candidate_metrics", _numeric_metrics(self.candidate_metrics))
        object.__setattr__(self, "primary_dimension", _bounded_text(self.primary_dimension, 120).strip() or None)
        if self.improvement is not None:
            try:
                improvement = float(self.improvement)
            except (TypeError, ValueError, OverflowError):
                improvement = None
            object.__setattr__(self, "improvement", improvement if improvement is not None and math.isfinite(improvement) else None)
        object.__setattr__(self, "gates", tuple(self.gates))
        object.__setattr__(self, "accepted", bool(self.accepted))
        object.__setattr__(self, "rollback_performed", bool(self.rollback_performed))
        object.__setattr__(self, "isolated", bool(self.isolated))
        object.__setattr__(self, "duration_sec", float(self.duration_sec) if math.isfinite(float(self.duration_sec)) else 0.0)
        object.__setattr__(self, "exit_code", int(self.exit_code) if self.exit_code is not None else None)
        object.__setattr__(self, "timed_out", bool(self.timed_out))
        object.__setattr__(self, "stdout_bytes", max(0, int(self.stdout_bytes)))
        object.__setattr__(self, "stderr_bytes", max(0, int(self.stderr_bytes)))
        object.__setattr__(self, "result_verified", bool(self.result_verified))
        object.__setattr__(self, "error", _bounded_text(self.error, 1000))
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "receipt_hash", self._compute_hash())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": HARNESS_SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "harness_version": self.harness_version,
            "fixture_hash": self.fixture_hash,
            "evaluator_hash": self.evaluator_hash,
            "mode": self.mode,
            "candidate_revision_id": self.candidate_revision_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "baseline_revision_id": self.baseline_revision_id,
            "baseline_fingerprint": self.baseline_fingerprint,
            "baseline_metrics": _thaw(self.baseline_metrics),
            "candidate_metrics": _thaw(self.candidate_metrics),
            "primary_dimension": self.primary_dimension,
            "improvement": self.improvement,
            "gates": [gate.to_dict() for gate in self.gates],
            "accepted": self.accepted,
            "outcome": self.outcome,
            "rollback_performed": self.rollback_performed,
            "rollback_target": self.rollback_target,
            "isolated": self.isolated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "result_verified": self.result_verified,
            "error": self.error,
            "metadata": _thaw(self.metadata),
        }

    def _compute_hash(self) -> str:
        return hashlib.sha256(_canonical(self._payload()).encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        return bool(self.receipt_hash) and self.receipt_hash == self._compute_hash()

    @property
    def hard_gates_passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def gate_results(self) -> Mapping[str, bool]:
        return MappingProxyType({gate.name: gate.passed for gate in self.gates})

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if not gate.passed)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["receipt_hash"] = self.receipt_hash
        return payload

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvaluationReceipt":
        """Restore a receipt only when its content hash is intact."""

        if not isinstance(data, Mapping):
            raise CandidateResultError("receipt payload must be an object")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != HARNESS_SCHEMA_VERSION:
            raise CandidateResultError("unsupported evaluation receipt schema")
        supplied_hash = _bounded_text(data.get("receipt_hash"), 128)
        raw_gates = data.get("gates", ())
        if not isinstance(raw_gates, (list, tuple)):
            raise CandidateResultError("receipt gates must be a list")
        gates = tuple(
            GateResult(
                name=item.get("name", ""),
                passed=item.get("passed", False),
                detail=item.get("detail", ""),
                evidence=item.get("evidence", {}),
            )
            for item in raw_gates
            if isinstance(item, Mapping)
        )
        payload = dict(data)
        payload.pop("schema_version", None)
        payload.pop("receipt_hash", None)
        payload["gates"] = gates
        try:
            restored = cls(**payload)
        except (TypeError, ValueError, EvaluationHarnessError) as exc:
            raise CandidateResultError("invalid receipt payload") from exc
        if not supplied_hash or supplied_hash != restored.receipt_hash:
            raise CandidateResultError("receipt hash mismatch")
        return restored

    @classmethod
    def from_json(cls, text: str) -> "EvaluationReceipt":
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateResultError("receipt JSON is invalid") from exc
        return cls.from_dict(payload)


def _numeric_metrics(value: Mapping[str, Any] | None) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    normalized: dict[str, float] = {}
    for raw_key, raw_value in list(value.items())[:64]:
        key = _bounded_text(raw_key, 120).strip()
        try:
            number = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if key and math.isfinite(number):
            normalized[key] = number
    return MappingProxyType(normalized)


def _make_tree_read_only(root: Path) -> None:
    """Best-effort read-only fixture permissions (hash checking is authoritative)."""

    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(current) / name
            try:
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
        for name in dirs:
            path = Path(current) / name
            try:
                path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
            except OSError:
                pass
    try:
        root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    except OSError:
        pass


def _fixture_tree_read_only(
    root: Path,
    *,
    budget: "ResourceBudget | None" = None,
) -> bool:
    """Check the permission bits set by :func:`_make_tree_read_only`.

    Windows ACLs may ignore POSIX mode bits, so this check is supplemental to
    the content hash.  On POSIX it catches a candidate that deliberately
    chmods a fixture writable before editing it; on Windows a failed write is
    still caught by the child exit/result gate or by the content hash.
    """

    try:
        if _is_link_like(root) or not root.is_dir():
            return False
        inspected_entries = 0
        max_entries = max(1024, int(budget.max_files) * 8) if budget is not None else 100_000
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            if current_path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                return False
            safe_dirs: list[str] = []
            for name in dirs:
                inspected_entries += 1
                if inspected_entries > max_entries:
                    return False
                path = current_path / name
                if _is_link_like(path) or path.stat().st_mode & (
                    stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
                ):
                    return False
                safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in files:
                inspected_entries += 1
                if inspected_entries > max_entries:
                    return False
                path = current_path / name
                if _is_link_like(path) or path.stat().st_mode & (
                    stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
                ):
                    return False
    except OSError:
        return False
    return True


def _copy_candidate(
    source: Path,
    destination: Path,
    *,
    budget: "ResourceBudget | None" = None,
) -> None:
    """Copy a candidate while enforcing limits before and during I/O."""

    destination.mkdir(parents=True, exist_ok=False)
    file_count = 0
    total_bytes = 0
    for path, rel in _iter_regular_files(source, budget=budget):
        if budget is not None and file_count >= budget.max_files:
            raise CandidateIsolationError("candidate file count exceeds the evaluation budget")
        try:
            expected_size = max(0, int(path.stat().st_size))
        except OSError as exc:
            raise CandidateIsolationError(f"cannot stat candidate file: {path}") from exc
        if budget is not None:
            if expected_size > budget.max_file_bytes:
                raise CandidateIsolationError(
                    f"candidate file exceeds max_file_bytes: {path}"
                )
            if total_bytes + expected_size > budget.max_total_bytes:
                raise CandidateIsolationError("candidate bytes exceed the evaluation budget")
        target = destination / Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = _copy_regular_file(
            path,
            target,
            max_bytes=budget.max_file_bytes if budget is not None else None,
        )
        if budget is not None and total_bytes + copied > budget.max_total_bytes:
            try:
                if _lexists(target) and not _is_link_like(target):
                    target.unlink()
            except OSError:
                pass
            raise CandidateIsolationError("candidate bytes exceed the evaluation budget")
        file_count += 1
        total_bytes += copied


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> int:
    """Copy one file through a checked descriptor, never ``copyfile`` paths.

    The directory walk is only a snapshot.  Opening the source and checking
    ``fstat`` closes the common check-then-replace race on platforms exposing
    ``O_NOFOLLOW``; Windows still requires the external sandbox/ACL contract
    described by the module because its standard ``os.open`` lacks an
    equivalent portable flag.
    """

    if _is_link_like(source):
        raise CandidateIsolationError(f"symlink/reparse source is not allowed: {source}")
    if _is_link_like(destination) or _lexists(destination):
        raise CandidateIsolationError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    copied_bytes = 0
    try:
        fd = os.open(str(source), flags)
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise CandidateIsolationError(f"non-regular source is not allowed: {source}")
        if getattr(source_stat, "st_nlink", 1) > 1:
            raise CandidateIsolationError(f"hard-linked source is not allowed: {source}")
        if max_bytes is not None and int(source_stat.st_size) > max_bytes:
            raise CandidateIsolationError(
                f"candidate file exceeds max_file_bytes: {source}"
            )
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
    except (OSError, ValueError) as exc:
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
        raise CandidateIsolationError(f"cannot copy isolated file: {source}") from exc
    return copied_bytes


def _tree_stats(
    root: Path,
    *,
    budget: "ResourceBudget | None" = None,
) -> tuple[int, int, bool]:
    """Return (file_count, total_bytes, sensitive_created_or_seen)."""

    count = total = 0
    sensitive = False
    root = _resolve_local_path(root, strict=True)
    inspected_entries = 0
    max_entries = max(1024, int(budget.max_files) * 8) if budget is not None else 100_000
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for name in dirs:
            inspected_entries += 1
            if inspected_entries > max_entries:
                return count, total, True
            if _is_link_like(current_path / name):
                sensitive = True
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in files:
            inspected_entries += 1
            if inspected_entries > max_entries:
                return count, total, True
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if _is_sensitive_name(rel):
                sensitive = True
                continue
            if _is_link_like(path):
                sensitive = True
                continue
            try:
                stat_result = path.stat()
                if getattr(stat_result, "st_nlink", 1) > 1:
                    # Treat aliases as a boundary violation even when the
                    # linked target is not named like a secret.  A candidate
                    # can otherwise create a hard link to an arbitrary host
                    # file after the initial copy.
                    sensitive = True
                size = stat_result.st_size
            except OSError:
                sensitive = True
                continue
            count += 1
            total += max(0, int(size))
            if budget is not None and (
                count > budget.max_files
                or int(size) > budget.max_file_bytes
                or total > budget.max_total_bytes
            ):
                return count, total, sensitive
    return count, total, sensitive


def _tree_within_budget(root: Path, budget: ResourceBudget) -> tuple[bool, int, int, bool]:
    """Check count/aggregate/per-file limits without following links."""

    count, total, sensitive = _tree_stats(root, budget=budget)
    if count > budget.max_files or total > budget.max_total_bytes or sensitive:
        return False, count, total, sensitive
    inspected_entries = 0
    max_entries = max(1024, int(budget.max_files) * 8)
    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_dirs: list[str] = []
            for name in dirs:
                inspected_entries += 1
                if inspected_entries > max_entries:
                    return False, count, total, True
                path = current_path / name
                if _is_link_like(path):
                    return False, count, total, True
                safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in files:
                inspected_entries += 1
                if inspected_entries > max_entries:
                    return False, count, total, True
                path = current_path / name
                if _is_link_like(path):
                    return False, count, total, True
                if path.stat().st_size > budget.max_file_bytes:
                    return False, count, total, sensitive
    except OSError:
        return False, count, total, True
    return True, count, total, sensitive


def _safe_environment(
    candidate: Path,
    fixtures: Path,
    budget: ResourceBudget,
    *,
    sandbox_temp: Path | None = None,
    harness_version: str = HARNESS_VERSION,
    execution_request: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build a minimal environment and remove credential-bearing variables."""

    allowed_names = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in allowed_names or _SENSITIVE_ENV_RE.search(upper):
            continue
        env[key] = value
    env.update(
        {
            "EVAL_CANDIDATE_DIR": str(candidate),
            "EVAL_FIXTURES_DIR": str(fixtures),
            "EVAL_HARNESS_VERSION": harness_version,
            "EVAL_TIMEOUT_SEC": str(budget.timeout_sec),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    if execution_request is not None:
        if not isinstance(execution_request, Mapping):
            raise HarnessConfigurationError("execution_request must be an object")
        # This is a small, non-secret envelope for an external executor.  It
        # is deliberately bounded before crossing the process boundary; the
        # immutable EvaluationReceipt records whatever verified metadata the
        # fixture judge returns.
        try:
            request_text = _canonical(execution_request)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HarnessConfigurationError("execution_request is not JSON-safe") from exc
        if len(request_text.encode("utf-8")) > 8 * 1024 or any(
            ord(char) < 32 and char not in "\r\n\t" for char in request_text
        ):
            raise HarnessConfigurationError("execution_request exceeds its bounded contract")
        env["P7_EXECUTION_REQUEST"] = request_text
    if sandbox_temp is not None:
        sandbox_temp.mkdir(parents=True, exist_ok=True)
        # Keep language/runtime temporary artifacts inside the disposable
        # evaluation root instead of inheriting a host-wide temp directory.
        env["TEMP"] = str(sandbox_temp)
        env["TMP"] = str(sandbox_temp)
    return env


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    budget: ResourceBudget,
    watch_roots: Sequence[Path] = (),
) -> tuple[int | None, bool, str, bytes, bytes, int, int, bool]:
    """Run a child while keeping captured output memory bounded.

    ``Popen.communicate`` stores an unbounded stream in memory.  A candidate
    that prints continuously must not be able to exhaust the evaluator, so
    reader threads retain at most ``limit + 1`` bytes while counting the full
    stream.  The extra byte makes an over-limit result unambiguous.
    """

    try:
        process_options: dict[str, Any] = {
            "cwd": str(cwd),
            "env": dict(env),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            # CREATE_SUSPENDED lets us assign the process to a kill-on-close
            # Job Object before any candidate code can spawn descendants.
            process_options["creationflags"] = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ) | 0x00000004
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(list(argv), **process_options)
    except (OSError, ValueError) as exc:
        return (
            None,
            False,
            f"candidate process failed: {type(exc).__name__}",
            b"",
            b"",
            0,
            0,
            False,
        )

    process_group = process.pid if os.name != "nt" else None
    job: Any = None

    def abort_setup(started_threads: Sequence[threading.Thread] = ()) -> None:
        """Best-effort cleanup for setup failures; callers retain the primary error."""

        nonlocal job
        current_job = job
        job = None
        try:
            _terminate_process_tree(
                process, job=current_job, process_group=process_group
            )
        except BaseException:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                _terminate_process_tree(process, process_group=process_group)
            except BaseException:
                pass
            try:
                process.wait(timeout=1.0)
            except BaseException:
                pass
        except BaseException:
            try:
                _terminate_process_tree(process, process_group=process_group)
            except BaseException:
                pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except BaseException:
                pass
        for thread in started_threads:
            try:
                thread.join(timeout=1.0)
            except BaseException:
                pass

    try:
        job = _create_kill_job(process)
        if os.name == "nt" and not job:
            abort_setup()
            return (
                None,
                False,
                "candidate process boundary unavailable",
                b"",
                b"",
                0,
                0,
                False,
            )
        if not _resume_suspended_process(process):
            abort_setup()
            return (
                None,
                False,
                "candidate process could not be resumed",
                b"",
                b"",
                0,
                0,
                False,
            )
    except BaseException:
        # Attaching/resuming is part of the isolation setup.  Never let a
        # setup exception strand a live candidate outside the boundary.
        abort_setup()
        raise

    captures: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    limits = {"stdout": budget.max_stdout_bytes, "stderr": budget.max_stderr_bytes}

    drain_errors = {"stdout": False, "stderr": False}

    def drain(name: str, pipe: Any) -> None:
        if pipe is None:
            drain_errors[name] = True
            return
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                totals[name] += len(chunk)
                remaining = limits[name] + 1 - len(captures[name])
                if remaining > 0:
                    captures[name].extend(chunk[:remaining])
        except Exception:
            drain_errors[name] = True
        except BaseException:
            # A reader interrupted asynchronously cannot attest that the
            # output boundary was completely consumed.
            drain_errors[name] = True
        finally:
            try:
                pipe.close()
            except BaseException:
                drain_errors[name] = True

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
    except BaseException:
        abort_setup(threads)
        raise
    timed_out = False
    run_error = ""
    monitor_stop = threading.Event()
    resource_exceeded = threading.Event()
    monitor_error = threading.Event()
    termination_lock = threading.Lock()
    termination_attempted = False
    termination_result = True
    job_holder = [job]

    def close_boundary() -> bool:
        """Close the process group/job exactly once and retain its result."""

        nonlocal termination_attempted, termination_result
        with termination_lock:
            if termination_attempted:
                return termination_result
            termination_attempted = True
            try:
                termination_result = _terminate_process_tree(
                    process,
                    job=job_holder[0],
                    process_group=process_group,
                )
            except BaseException:
                termination_result = False
            finally:
                # A closed Job handle must never be reused by another thread.
                job_holder[0] = None
            return termination_result

    def close_boundary_safely() -> bool:
        """Keep cleanup progressing if the lock/termination path is interrupted."""

        nonlocal termination_result
        try:
            return close_boundary()
        except BaseException:
            termination_result = False
            # A lock/interruption before ``close_boundary`` reaches the kill
            # primitive must not leave the candidate tree running.
            try:
                _terminate_process_tree(process, process_group=process_group)
            except BaseException:
                pass
            return False

    def monitor_artifacts() -> None:
        """Stop a candidate while it grows an isolated tree past its limits.

        The monitor is a cooperative safety net for hosts that do not provide
        an OS quota.  It closes the common long-running disk-exhaustion path;
        a container/job quota is still required against a hostile process
        that can write faster than the polling interval.
        """

        if not watch_roots:
            return
        interval = min(0.1, max(0.01, budget.timeout_sec / 100.0))
        while not monitor_stop.wait(interval):
            for watched in watch_roots:
                try:
                    within, _count, _total, _unsafe = _tree_within_budget(
                        Path(watched), budget
                    )
                except (OSError, EvaluationHarnessError):
                    within = False
                if not within:
                    resource_exceeded.set()
                    close_boundary_safely()
                    return
            # A monitor exception is itself an unverifiable resource state;
            # do not let a daemon-thread traceback look like a clean run.

    # Keep the monitor's own asynchronous failures observable to the caller.
    original_monitor = monitor_artifacts

    def guarded_monitor() -> None:
        try:
            original_monitor()
        except BaseException:
            monitor_error.set()
            close_boundary_safely()

    monitor_thread = threading.Thread(
        target=guarded_monitor,
        name="brain-memory-eval-budget",
        daemon=True,
    )
    try:
        monitor_thread.start()
    except BaseException:
        # Thread.start can fail after the candidate and pipe readers exist.
        # Close every boundary before preserving the start exception.
        try:
            monitor_stop.set()
        except BaseException:
            pass
        close_boundary_safely()
        try:
            process.wait(timeout=2.0)
        except BaseException:
            close_boundary_safely()
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except BaseException:
                pass
        for thread in threads:
            try:
                thread.join(timeout=1.0)
            except BaseException:
                pass
        raise
    cleanup_error: BaseException | None = None
    try:
        deadline = time.monotonic() + budget.timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                run_error = f"candidate exceeded timeout {budget.timeout_sec:g}s"
                close_boundary_safely()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    run_error = f"candidate could not be stopped after timeout {budget.timeout_sec:g}s"
                break
            try:
                process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                if resource_exceeded.is_set():
                    run_error = "candidate exceeded isolated artifact resource budget"
                    break
    except BaseException:
        # Never abandon a candidate tree when the supervising thread is
        # interrupted.  The original exception is deliberately propagated.
        close_boundary_safely()
        raise
    finally:
        try:
            monitor_stop.set()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
            monitor_error.set()
        # Close the group/job even after a normal parent exit: a descendant
        # may have redirected its output and otherwise outlive the parent.
        close_boundary_safely()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            run_error = run_error or "candidate process boundary did not close"
            close_boundary_safely()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
            run_error = run_error or "candidate process boundary did not close"
            close_boundary_safely()
        try:
            monitor_thread.join(timeout=2.0)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
            monitor_error.set()
        for thread in threads:
            try:
                thread.join(timeout=2.0)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                drain_errors["stdout" if thread is threads[0] else "stderr"] = True
            try:
                thread_alive = thread.is_alive()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                thread_alive = True
                drain_errors["stdout" if thread is threads[0] else "stderr"] = True
            if thread_alive:
                drain_errors["stdout" if thread is threads[0] else "stderr"] = True
                for stream in (process.stdout, process.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except BaseException:
                        drain_errors["stdout" if thread is threads[0] else "stderr"] = True
                        break
                try:
                    thread.join(timeout=1.0)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    drain_errors["stdout" if thread is threads[0] else "stderr"] = True
    if cleanup_error is not None:
        raise cleanup_error
    try:
        monitor_alive = monitor_thread.is_alive()
    except BaseException:
        monitor_alive = True
        monitor_error.set()
    thread_alive_states: list[bool] = []
    for thread in threads:
        try:
            thread_alive_states.append(thread.is_alive())
        except BaseException:
            thread_alive_states.append(True)
            drain_errors["stdout" if thread is threads[0] else "stderr"] = True
    if resource_exceeded.is_set() and not run_error:
        run_error = "candidate exceeded isolated artifact resource budget"
    if monitor_alive or monitor_error.is_set():
        run_error = run_error or "candidate resource monitor boundary unverified"
    if any(drain_errors.values()) or any(thread_alive_states):
        run_error = run_error or "candidate output boundary unverified"
    boundary_closed = bool(
        termination_attempted
        and termination_result
        and process.returncode is not None
        and not monitor_alive
        and not monitor_error.is_set()
        and not any(drain_errors.values())
        and not any(thread_alive_states)
    )
    return (
        process.returncode,
        timed_out,
        run_error,
        bytes(captures["stdout"]),
        bytes(captures["stderr"]),
        totals["stdout"],
        totals["stderr"],
        boundary_closed,
    )


def _create_kill_job(process: subprocess.Popen[Any]) -> Any:
    """Attach a Windows kill-on-close Job Object to an evaluator child."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        set_info.restype = wintypes.BOOL
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        handle = None
        try:
            handle = create_job(None, None)
            if not handle:
                return None
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not set_info(
                handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) or not assign(handle, wintypes.HANDLE(process._handle)):
                return None
            job = (kernel32, handle)
            handle = None
            return job
        finally:
            if handle:
                try:
                    close(handle)
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _close_kill_job(job: Any) -> bool:
    if not job:
        return True
    try:
        return bool(job[0].CloseHandle(job[1]))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _resume_suspended_process(process: subprocess.Popen[Any]) -> bool:
    """Resume a Windows child only after it entered its Job Object."""

    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        resume = ntdll.NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        return int(resume(wintypes.HANDLE(process._handle))) == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _terminate_process_tree(
    process: subprocess.Popen[Any], *, job: Any = None, process_group: int | None = None
) -> bool:
    """Close a child tree and report whether the boundary was proven closed."""

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    boundary_closed = True
    if job:
        # Job close is authoritative.  taskkill is cleanup-only fallback and
        # therefore cannot turn a failed close into a positive attestation.
        boundary_closed = _close_kill_job(job)
        if not boundary_closed and os.name == "nt":
            try:
                system_root = os.environ.get("SystemRoot", r"C:\\Windows")
                taskkill = Path(system_root) / "System32" / "taskkill.exe"
                if taskkill.is_file() and not _is_link_like(taskkill):
                    result = subprocess.run(
                        [str(taskkill), "/PID", str(pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        close_fds=True,
                        timeout=3,
                        check=False,
                    )
                    if result.returncode != 0:
                        boundary_closed = False
            except (OSError, ValueError, subprocess.TimeoutExpired):
                boundary_closed = False
    elif os.name == "nt":
        # Without a Job Object the descendant boundary is not attestable.
        boundary_closed = False
        try:
            system_root = os.environ.get("SystemRoot", r"C:\\Windows")
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            if taskkill.is_file() and not _is_link_like(taskkill):
                result = subprocess.run(
                    [str(taskkill), "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    close_fds=True,
                    timeout=3,
                    check=False,
                )
                if result.returncode != 0:
                    boundary_closed = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            boundary_closed = False
    else:
        try:
            os.killpg(process_group or pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (AttributeError, OSError):
            boundary_closed = False
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except (AttributeError, OSError, ValueError):
        boundary_closed = False
    return boundary_closed


def _expand_command(command: Sequence[str] | str | None, candidate: Path, fixtures: Path) -> list[str]:
    if command is None:
        for name in ("evaluate.py", "run.py", "candidate.py"):
            if (candidate / name).is_file():
                return [sys.executable, str(candidate / name), "--fixtures", str(fixtures)]
        raise HarnessConfigurationError(
            "no command supplied and candidate has no evaluate.py/run.py/candidate.py"
        )
    if isinstance(command, str):
        try:
            tokens = shlex.split(command, posix=(os.name != "nt"))
        except ValueError as exc:
            raise HarnessConfigurationError("invalid command string") from exc
    else:
        tokens = [str(item) for item in command]
    if not tokens:
        raise HarnessConfigurationError("candidate command must not be empty")
    replacements = {
        "{candidate}": str(candidate),
        "{fixtures}": str(fixtures),
        "{fixture}": str(fixtures),
    }
    expanded: list[str] = []
    for token in tokens:
        if "\x00" in token or "\n" in token or "\r" in token:
            raise HarnessConfigurationError("candidate command contains control characters")
        for marker, value in replacements.items():
            token = token.replace(marker, value)
        expanded.append(token)
    _validate_command(expanded, candidate, fixtures)
    return expanded


def _validate_command_template(command: Sequence[str] | str | None) -> None:
    """Validate command tokens that do not depend on an isolated path yet."""

    if command is None:
        return
    if isinstance(command, str):
        try:
            tokens = shlex.split(command, posix=(os.name != "nt"))
        except ValueError as exc:
            raise HarnessConfigurationError("invalid command string") from exc
    else:
        tokens = [str(item) for item in command]
    if not tokens:
        raise HarnessConfigurationError("candidate command must not be empty")
    shell_names = {
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "sh", "bash", "zsh", "fish",
    }
    if Path(tokens[0]).name.lower() in shell_names:
        raise HarnessConfigurationError("shell wrappers are not allowed for candidate evaluation")
    for token in tokens:
        lowered = token.lower()
        if "\x00" in token or "\n" in token or "\r" in token:
            raise HarnessConfigurationError("candidate command contains control characters")
        if re.search(r"(?:https?://|github\.com/|git(?:\.exe)?\s+(?:clone|push|fetch))", lowered):
            raise HarnessConfigurationError("remote/GitHub command is outside the local evaluation contract")
        if lowered in {"-c", "/c", "-command", "--command", "-enc", "-encodedcommand"}:
            raise HarnessConfigurationError("shell/eval command flags are not allowed")


def _validate_command(argv: Sequence[str], candidate: Path, fixtures: Path) -> None:
    """Reject obvious shell/remote escapes before creating the child process.

    This is intentionally a small portable guard, not a substitute for an OS
    sandbox.  The child is always launched with ``shell=False`` and a local
    working directory.  Absolute script paths are restricted to the copied
    candidate/fixture trees (the executable itself may be the configured
    Python/runtime binary).
    """

    if not argv:
        raise HarnessConfigurationError("candidate command must not be empty")
    shell_names = {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "bash",
        "zsh",
        "fish",
    }
    first_name = Path(argv[0]).name.lower()
    if first_name in shell_names:
        raise HarnessConfigurationError("shell wrappers are not allowed for candidate evaluation")
    for index, token in enumerate(argv):
        lowered = token.lower()
        if "\x00" in token or re.search(r"(?:https?://|github\.com/|git(?:\.exe)?\s+(?:clone|push|fetch))", lowered):
            raise HarnessConfigurationError("remote/GitHub command is outside the local evaluation contract")
        if index == 0:
            if Path(token).is_absolute():
                try:
                    _resolve_local_path(token, strict=False)
                except (OSError, HarnessConfigurationError) as exc:
                    raise HarnessConfigurationError("invalid local executable path") from exc
            continue
        if lowered in {"-c", "/c", "-command", "--command", "-enc", "-encodedcommand"}:
            raise HarnessConfigurationError("shell/eval command flags are not allowed")
        path = Path(token)
        if not path.is_absolute():
            continue
        try:
            resolved = _resolve_local_path(path, strict=False)
            candidate_root = _resolve_local_path(candidate, strict=False)
            fixture_root = _resolve_local_path(fixtures, strict=False)
            runtime = _resolve_local_path(sys.executable, strict=False)
            inside_candidate = resolved == candidate_root or candidate_root in resolved.parents
            inside_fixtures = resolved == fixture_root or fixture_root in resolved.parents
            if resolved != runtime and not inside_candidate and not inside_fixtures:
                raise HarnessConfigurationError(
                    "absolute command paths must remain inside the isolated trees"
                )
        except (OSError, HarnessConfigurationError) as exc:
            raise HarnessConfigurationError("invalid absolute command path") from exc


def _command_has_independent_judge(
    argv: Sequence[str], candidate: Path, fixtures: Path
) -> bool:
    """Return true only when executable judge code comes from fixed fixtures.

    The candidate path may be passed as data, but a script/module inside that
    path cannot serve as its own judge.  A fixture-owned judge is content-
    hashed before and after the run, so changing the admission rule changes
    the fixture hash and invalidates the configured harness.
    """

    candidate_root = _resolve_local_path(candidate, strict=False)
    fixture_root = _resolve_local_path(fixtures, strict=False)
    fixture_code_seen = False
    for index, token in enumerate(argv):
        if token.startswith("-"):
            continue
        path = Path(token)
        if not path.is_absolute():
            # Relative executable/script tokens resolve from the candidate
            # cwd and are therefore candidate-controlled.
            if index > 0 and path.suffix.lower() in {".py", ".pyw", ".exe", ".bat", ".cmd"}:
                return False
            continue
        resolved = _resolve_local_path(path, strict=False)
        inside_candidate = resolved == candidate_root or candidate_root in resolved.parents
        inside_fixtures = resolved == fixture_root or fixture_root in resolved.parents
        if inside_candidate and path.suffix.lower() in {".py", ".pyw", ".exe", ".bat", ".cmd"}:
            return False
        if inside_fixtures and path.suffix.lower() in {".py", ".pyw", ".exe"}:
            fixture_code_seen = True
    return fixture_code_seen


def _parse_candidate_result(stdout: bytes) -> tuple[dict[str, Any] | None, str]:
    if not stdout:
        return None, "candidate produced no result"
    text = stdout.decode("utf-8", errors="replace")
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(RESULT_PREFIX):
            candidates.append(line[len(RESULT_PREFIX) :].strip())
    if not candidates:
        stripped = text.strip()
        if stripped:
            candidates.append(stripped)
            # A small amount of diagnostic logging before a JSON object is
            # tolerated by selecting the last non-empty line.
            if "\n" in stripped:
                candidates.append(stripped.splitlines()[-1].strip())
    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed, ""
    return None, "candidate result is not a JSON object"


def _coerce_baseline(
    value: BaselineRevision | Mapping[str, Any] | str | os.PathLike[str] | None,
    *,
    budget: "ResourceBudget | None" = None,
) -> BaselineRevision | None:
    if value is None:
        return None
    if isinstance(value, BaselineRevision):
        return value
    if isinstance(value, (str, os.PathLike)):
        return BaselineRevision.from_path(value, budget=budget)
    if isinstance(value, Mapping):
        descriptor_keys = {"revision_id", "id", "fingerprint", "sha256", "metrics", "source_path"}
        if not any(key in value for key in descriptor_keys):
            # Convenience form: ``{"quality": 0.5, ...}``.  The digest is
            # over the explicit metric payload, so it remains auditable even
            # when no source directory is available.
            metrics = _numeric_metrics(value)
            if not metrics:
                raise HarnessConfigurationError("inline baseline metrics are empty")
            fingerprint = hashlib.sha256(_canonical(_thaw(metrics)).encode("utf-8")).hexdigest()
            return BaselineRevision("baseline-inline", fingerprint, metrics)
        return BaselineRevision(
            revision_id=value.get("revision_id", value.get("id", "baseline")),
            fingerprint=value.get("fingerprint", value.get("sha256", "")),
            metrics=value.get("metrics", {}),
            source_path=value.get("source_path"),
            harness_version=value.get("harness_version"),
            fixture_hash=value.get("fixture_hash"),
            evaluator_hash=value.get("evaluator_hash"),
        )
    raise HarnessConfigurationError("unsupported baseline descriptor")


class EvaluationHarness:
    """Fixed evaluator for local candidate revisions.

    Parameters are intentionally explicit.  ``fixtures``/``fixture_dir`` may
    point only to a local directory.  ``command`` is an argv sequence (or a
    shell-like string parsed without a shell); use ``{candidate}`` and
    ``{fixtures}`` placeholders to pass the isolated paths to a fixture-owned
    judge child.  The command must contain an executable/script under the
    copied fixture tree; a candidate-owned script is never an acceptance
    authority.
    """

    _DEFAULT_CRITICAL_DIMENSIONS = ()

    def __init__(
        self,
        fixtures: str | os.PathLike[str] | None = None,
        *,
        fixture_dir: str | os.PathLike[str] | None = None,
        fixture_root: str | os.PathLike[str] | None = None,
        harness_version: str = HARNESS_VERSION,
        command: Sequence[str] | str | None = None,
        judge_command: Sequence[str] | str | None = None,
        resource_budget: ResourceBudget | None = None,
        budget: ResourceBudget | None = None,
        primary_dimension: str | None = None,
        critical_dimensions: Sequence[str] | None = None,
        improvement_epsilon: float = 1e-9,
    ) -> None:
        fixture_aliases = [item for item in (fixtures, fixture_dir, fixture_root) if item is not None]
        if fixture_aliases:
            resolved_aliases = {str(_resolve_local_path(item, strict=False)) for item in fixture_aliases}
            if len(resolved_aliases) > 1:
                raise HarnessConfigurationError("fixtures/fixture_dir/fixture_root disagree")
        if resource_budget is not None and budget is not None and resource_budget != budget:
            raise HarnessConfigurationError("resource_budget and budget disagree")
        self._budget = resource_budget or budget or ResourceBudget()
        fixture_source = fixture_dir if fixture_dir is not None else fixture_root if fixture_root is not None else fixtures
        # Parse and hash fixtures only after the finite budget is known.  A
        # fixture directory is evaluator-owned input, but it is still an
        # external tree and must not be allowed to consume unbounded memory or
        # I/O during harness construction.
        self._fixtures = FixtureSet.from_path(fixture_source, budget=self._budget)
        self._harness_version = _bounded_text(harness_version, 80).strip() or HARNESS_VERSION
        if command is not None and judge_command is not None and command != judge_command:
            raise HarnessConfigurationError("command and judge_command disagree")
        selected_template = judge_command if judge_command is not None else command
        _validate_command_template(selected_template)
        self._command = selected_template
        self._primary_dimension = _bounded_text(primary_dimension, 120).strip() or None
        dims = critical_dimensions if critical_dimensions is not None else self._DEFAULT_CRITICAL_DIMENSIONS
        self._critical_dimensions = tuple(
            item for item in (_bounded_text(value, 120).strip() for value in dims) if item
        )
        try:
            epsilon = float(improvement_epsilon)
        except (TypeError, ValueError, OverflowError):
            raise HarnessConfigurationError("improvement_epsilon must be finite") from None
        if not math.isfinite(epsilon) or epsilon < 0:
            raise HarnessConfigurationError("improvement_epsilon must be finite and non-negative")
        self._improvement_epsilon = epsilon
        self._evaluator_hash = self._compute_evaluator_hash()

    @property
    def harness_version(self) -> str:
        return self._harness_version

    @property
    def fixture_hash(self) -> str:
        return self._fixtures.digest

    @property
    def fixture_manifest_hash(self) -> str:
        return self._fixtures.digest

    @property
    def evaluator_hash(self) -> str:
        return self._evaluator_hash

    @property
    def resource_budget(self) -> ResourceBudget:
        return self._budget

    @property
    def fixtures(self) -> FixtureSet:
        return self._fixtures

    def _compute_evaluator_hash(self) -> str:
        try:
            source = Path(inspect.getfile(type(self))).resolve(strict=True)
            data = source.read_bytes()
        except (OSError, TypeError):
            data = inspect.getsource(type(self)).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _new_receipt(
        self,
        *,
        started_at: str,
        finished_at: str,
        duration_sec: float,
        candidate: CandidateRevision,
        baseline: BaselineRevision | None,
        mode: EvaluationMode,
        metrics: Mapping[str, float] | None,
        primary_dimension: str | None,
        improvement: float | None,
        gates: Iterable[GateResult],
        accepted: bool,
        outcome: str,
        rollback_performed: bool,
        exit_code: int | None,
        timed_out: bool,
        stdout_bytes: int,
        stderr_bytes: int,
        result_verified: bool,
        error: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationReceipt:
        return EvaluationReceipt(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            harness_version=self._harness_version,
            fixture_hash=self._fixtures.digest,
            evaluator_hash=self._evaluator_hash,
            mode=mode.value,
            candidate_revision_id=candidate.revision_id,
            candidate_fingerprint=candidate.fingerprint,
            baseline_revision_id=baseline.revision_id if baseline else None,
            baseline_fingerprint=baseline.fingerprint if baseline else None,
            baseline_metrics=baseline.metrics if baseline else {},
            candidate_metrics=metrics or {},
            primary_dimension=primary_dimension,
            improvement=improvement,
            gates=tuple(gates),
            accepted=accepted,
            outcome=outcome,
            rollback_performed=rollback_performed,
            rollback_target=baseline.revision_id if baseline else None,
            isolated=True,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=duration_sec,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            result_verified=result_verified,
            error=error,
            metadata=metadata or {},
        )

    def evaluate(
        self,
        candidate: str | os.PathLike[str] | CandidateRevision,
        baseline: BaselineRevision | Mapping[str, Any] | str | os.PathLike[str] | None = None,
        *,
        mode: EvaluationMode | str = EvaluationMode.EVOLUTION,
        command: Sequence[str] | str | None = None,
        judge_command: Sequence[str] | str | None = None,
        primary_dimension: str | None = None,
        execution_request: Mapping[str, Any] | None = None,
    ) -> EvaluationReceipt:
        """Run one candidate and return an immutable acceptance receipt.

        Configuration/descriptor errors raise immediately.  Candidate runtime
        failures return a rejected receipt so callers can persist evidence and
        choose Recovery or Succession without catching process exceptions.
        """

        evaluation_mode = EvaluationMode.parse(mode)
        trusted_baseline = _coerce_baseline(baseline, budget=self._budget)
        if isinstance(candidate, CandidateRevision):
            candidate_descriptor = candidate
            source = _resolve_local_path(candidate.source_path, strict=True)
            if directory_fingerprint(source, budget=self._budget) != candidate.fingerprint:
                raise CandidateIsolationError("candidate descriptor fingerprint is stale")
        else:
            source = _resolve_local_path(candidate, strict=True)
            candidate_descriptor = CandidateRevision.from_path(source, budget=self._budget)
        if not source.is_dir():
            raise CandidateIsolationError("candidate must be a directory")
        if evaluation_mode is EvaluationMode.EVOLUTION and trusted_baseline is None:
            raise HarnessConfigurationError("evolution evaluation requires a trusted baseline")
        if evaluation_mode is EvaluationMode.RECOVERY and trusted_baseline is None:
            raise HarnessConfigurationError("recovery evaluation requires a trusted baseline")

        started_at = _utc_now()
        started_clock = time.monotonic()
        if command is not None and judge_command is not None and command != judge_command:
            raise HarnessConfigurationError("command and judge_command disagree")
        selected_command = (
            judge_command
            if judge_command is not None
            else command if command is not None else self._command
        )
        baseline_before = None
        baseline_observed_ok = True
        if trusted_baseline and trusted_baseline.source_path:
            try:
                baseline_before = directory_fingerprint(
                    trusted_baseline.source_path,
                    budget=self._budget,
                )
                baseline_observed_ok = baseline_before == trusted_baseline.fingerprint
            except (OSError, EvaluationHarnessError):
                baseline_before = None
                baseline_observed_ok = False

        gates: list[GateResult] = []
        metrics: Mapping[str, float] = {}
        primary: str | None = _bounded_text(primary_dimension, 120).strip() or self._primary_dimension
        improvement: float | None = None
        exit_code: int | None = None
        timed_out = False
        stdout_bytes = stderr_bytes = 0
        result_verified = False
        error = ""
        startup_ok = False
        boundary_closed = False
        fixture_ok = False
        fixture_source_ok = False
        candidate_source_ok = False
        evaluator_ok = False
        resources_ok = False
        permissions_ok = False
        independent_judge_ok = False
        baseline_contract_ok = True
        sandbox_boundary_ok = False
        parsed_result: dict[str, Any] | None = None
        baseline_unchanged = True

        try:
            if trusted_baseline is not None:
                baseline_contract_ok = all(
                    expected is None or expected == actual
                    for expected, actual in (
                        (trusted_baseline.harness_version, self._harness_version),
                        (trusted_baseline.fixture_hash, self._fixtures.digest),
                        (trusted_baseline.evaluator_hash, self._evaluator_hash),
                    )
                )
                if not baseline_contract_ok:
                    error = "baseline was produced by a different evaluator contract"
            # Detect source/fixture drift before granting a candidate a run.
            current_fixture_hash = (
                directory_fingerprint(self._fixtures.source, budget=self._budget)
                if self._fixtures.source is not None
                else EMPTY_FIXTURE_HASH
            )
            fixture_source_ok = current_fixture_hash == self._fixtures.digest
            if not fixture_source_ok:
                error = "fixture source changed after harness initialization"
            with tempfile.TemporaryDirectory(prefix="brain-memory-eval-") as temp_root:
                root = Path(temp_root)
                isolated_candidate = root / "candidate"
                isolated_fixtures = root / "fixtures"
                _copy_candidate(source, isolated_candidate, budget=self._budget)
                candidate_after_copy = directory_fingerprint(source, budget=self._budget)
                candidate_copy_fingerprint = directory_fingerprint(
                    isolated_candidate,
                    budget=self._budget,
                )
                if candidate_after_copy != candidate_descriptor.fingerprint:
                    raise CandidateIsolationError("candidate changed while being copied")
                # The copy must represent exactly the source files visible to
                # the evaluator.  Sensitive files are intentionally omitted.
                if candidate_copy_fingerprint != candidate_descriptor.fingerprint:
                    # A source with skipped private files has a different
                    # manifest only if the source changed or contains links;
                    # treat that as an integrity failure, not a best guess.
                    raise CandidateIsolationError("candidate copy fingerprint mismatch")
                self._fixtures.materialize(isolated_fixtures, budget=self._budget)
                fixture_copy_ok_before = self._fixtures.verify(
                    isolated_fixtures,
                    budget=self._budget,
                )
                if not fixture_source_ok or not fixture_copy_ok_before:
                    raise CandidateIsolationError("fixture integrity check failed before run")
                evaluator_before = self._compute_evaluator_hash()
                argv = _expand_command(selected_command, isolated_candidate, isolated_fixtures)
                independent_judge_ok = _command_has_independent_judge(
                    argv, isolated_candidate, isolated_fixtures
                )
                env = _safe_environment(
                    isolated_candidate,
                    isolated_fixtures,
                    self._budget,
                    sandbox_temp=root / "tmp",
                    harness_version=self._harness_version,
                    execution_request=execution_request,
                )
                (
                    exit_code,
                    timed_out,
                    run_error,
                    stdout,
                    stderr,
                    stdout_bytes,
                    stderr_bytes,
                    boundary_closed,
                ) = _run_bounded_process(
                    argv,
                    cwd=isolated_candidate,
                    env=env,
                    budget=self._budget,
                    watch_roots=(isolated_candidate, root / "tmp"),
                )
                startup_ok = bool(
                    not timed_out
                    and not run_error
                    and exit_code == 0
                    and boundary_closed
                )
                parsed_result, parse_error = _parse_candidate_result(stdout)
                if parsed_result is None:
                    error = run_error or parse_error
                elif run_error:
                    error = run_error
                else:
                    raw_metrics = parsed_result.get("metrics", {})
                    metrics = _numeric_metrics(raw_metrics)
                    result_verified = parsed_result.get("verified") is True
                    raw_status = parsed_result.get("status")
                    normalized_status = raw_status.lower() if isinstance(raw_status, str) else raw_status
                    if normalized_status not in (None, "pass", "accepted", "ok", "success", True):
                        startup_ok = False
                        error = _bounded_text(parsed_result.get("error", "candidate reported failure"), 1000)
                    if not result_verified and not error:
                        error = "candidate result is not explicitly verified"
                    raw_primary = parsed_result.get("primary_dimension")
                    if not primary and raw_primary:
                        primary = _bounded_text(raw_primary, 120).strip() or None

                fixture_ok = self._fixtures.verify(
                    isolated_fixtures,
                    budget=self._budget,
                ) and _fixture_tree_read_only(
                    isolated_fixtures,
                    budget=self._budget,
                )
                try:
                    candidate_source_ok = (
                        directory_fingerprint(source, budget=self._budget)
                        == candidate_descriptor.fingerprint
                    )
                    fixture_source_ok = (
                        (
                            directory_fingerprint(
                                self._fixtures.source,
                                budget=self._budget,
                            )
                            if self._fixtures.source is not None
                            else EMPTY_FIXTURE_HASH
                        )
                        == self._fixtures.digest
                    )
                except (OSError, EvaluationHarnessError):
                    candidate_source_ok = fixture_source_ok = False
                evaluator_ok = evaluator_before == self._compute_evaluator_hash() == self._evaluator_hash
                tree_ok, file_count, total_bytes, sensitive = _tree_within_budget(
                    isolated_candidate, self._budget
                )
                tmp_tree_ok, tmp_file_count, tmp_total_bytes, tmp_sensitive = _tree_within_budget(
                    root / "tmp", self._budget
                )
                unexpected_root_entries = [
                    item.name
                    for item in root.iterdir()
                    if item.name not in {"candidate", "fixtures", "tmp"}
                ]
                sandbox_boundary_ok = not unexpected_root_entries
                permissions_ok = not sensitive
                permissions_ok = permissions_ok and not tmp_sensitive and sandbox_boundary_ok
                resources_ok = tree_ok and tmp_tree_ok and (
                    not timed_out
                    and stdout_bytes <= self._budget.max_stdout_bytes
                    and stderr_bytes <= self._budget.max_stderr_bytes
                    and stdout_bytes + stderr_bytes <= self._budget.max_output_bytes
                    and file_count + tmp_file_count <= self._budget.max_files
                    and total_bytes + tmp_total_bytes <= self._budget.max_total_bytes
                )
                baseline_unchanged = True
                if trusted_baseline and trusted_baseline.source_path and baseline_before:
                    try:
                        baseline_unchanged = (
                            directory_fingerprint(
                                trusted_baseline.source_path,
                                budget=self._budget,
                            )
                            == baseline_before
                        )
                    except (OSError, EvaluationHarnessError):
                        baseline_unchanged = False
                elif trusted_baseline and trusted_baseline.source_path:
                    baseline_unchanged = False
                # The temporary root disappears here; no promotion/write-back
                # operation is ever performed by this module.
        except HarnessConfigurationError:
            # Unsafe evaluator configuration is a caller error, not a
            # candidate result.  Do not hide it inside a rejection receipt.
            raise
        except (OSError, EvaluationHarnessError) as exc:
            error = error or _bounded_text(str(exc), 1000)
            fixture_ok = fixture_source_ok = candidate_source_ok = False
            evaluator_ok = self._compute_evaluator_hash() == self._evaluator_hash
            permissions_ok = False if isinstance(exc, CandidateIsolationError) else permissions_ok
            startup_ok = False
            resources_ok = False
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            error = error or f"harness execution failed: {type(exc).__name__}"
            startup_ok = fixture_ok = fixture_source_ok = candidate_source_ok = evaluator_ok = resources_ok = permissions_ok = independent_judge_ok = baseline_contract_ok = sandbox_boundary_ok = False

        finished_at = _utc_now()
        duration_sec = max(0.0, time.monotonic() - started_clock)
        if duration_sec > self._budget.timeout_sec:
            resources_ok = False

        gates.append(GateResult(HardGate.STARTUP.value, startup_ok, "process exited successfully" if startup_ok else (error or "startup failed"), {"exit_code": exit_code, "timed_out": timed_out, "boundary_closed": boundary_closed}))
        integrity_ok = fixture_ok and fixture_source_ok and candidate_source_ok and evaluator_ok and baseline_observed_ok
        gates.append(GateResult(HardGate.INTEGRITY.value, integrity_ok, "fixture, baseline, candidate source, and evaluator hashes unchanged" if integrity_ok else "fixture/baseline/candidate/evaluator mutation or drift detected", {"fixture_copy_unchanged": fixture_ok, "fixture_source_unchanged": fixture_source_ok, "candidate_source_unchanged": candidate_source_ok, "baseline_fingerprint_matches": baseline_observed_ok, "evaluator_unchanged": evaluator_ok}))
        gates.append(GateResult(HardGate.RESOURCE_BUDGET.value, resources_ok, "within declared resource budget" if resources_ok else "resource budget exceeded", {"duration_sec": round(duration_sec, 6), "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes}))
        gates.append(GateResult(HardGate.PERMISSION_BOUNDARY.value, permissions_ok, "writes stayed inside disposable sandbox" if permissions_ok else "unsafe link, sensitive file, or sandbox-boundary write observed", {"sandbox_root_clean": sandbox_boundary_ok}))
        gates.append(GateResult(HardGate.INDEPENDENT_JUDGE.value, independent_judge_ok, "result emitted by fixture-owned judge" if independent_judge_ok else "candidate-controlled code cannot judge or promote itself", {}))
        gates.append(GateResult(HardGate.BASELINE_CONTRACT.value, baseline_contract_ok, "baseline uses the same fixed evaluator/fixture contract" if baseline_contract_ok else "baseline evaluator or fixture hash does not match", {"baseline_harness_version": trusted_baseline.harness_version if trusted_baseline else None, "current_harness_version": self._harness_version, "baseline_fixture_hash": trusted_baseline.fixture_hash if trusted_baseline else None, "baseline_evaluator_hash": trusted_baseline.evaluator_hash if trusted_baseline else None}))
        gates.append(GateResult(HardGate.VERIFICATION.value, result_verified, "independent judge supplied explicit verified result" if result_verified else "missing verified judge result", {}))

        # Progress is calculated only from finite, explicit metrics.  A
        # candidate cannot manufacture progress by changing the evaluator.
        if trusted_baseline and metrics:
            if not primary:
                primary = next(iter(trusted_baseline.metrics), None)
            if primary and primary in metrics and primary in trusted_baseline.metrics:
                improvement = metrics[primary] - trusted_baseline.metrics[primary]
        if evaluation_mode is EvaluationMode.EVOLUTION:
            progress_ok = bool(
                trusted_baseline
                and primary
                and primary in metrics
                and primary in trusted_baseline.metrics
                and improvement is not None
                and improvement > self._improvement_epsilon
            )
            if progress_ok and trusted_baseline:
                critical = self._critical_dimensions or tuple(trusted_baseline.metrics.keys())
                progress_ok = all(
                    dim in metrics
                    and dim in trusted_baseline.metrics
                    and metrics[dim] >= trusted_baseline.metrics[dim] - self._improvement_epsilon
                    for dim in critical
                )
            gates.append(GateResult(HardGate.IMPROVEMENT.value, progress_ok, "strict primary-dimension improvement with no critical regression" if progress_ok else "no reproducible improvement or critical regression", {"primary_dimension": primary, "improvement": improvement}))
        else:
            critical = self._critical_dimensions or tuple(trusted_baseline.metrics.keys()) if trusted_baseline else ()
            recovery_ok = bool(
                trusted_baseline
                and critical
                and all(
                    dim in metrics
                    and dim in trusted_baseline.metrics
                    and metrics[dim] >= trusted_baseline.metrics[dim] - self._improvement_epsilon
                    for dim in critical
                )
            )
            gates.append(GateResult(HardGate.RECOVERY_BASELINE.value, recovery_ok, "baseline metrics restored" if recovery_ok else "trusted baseline was not restored", {"dimensions": list(critical)}))

        rollback_ok = baseline_observed_ok and baseline_unchanged
        gates.append(GateResult(HardGate.ROLLBACK.value, rollback_ok, "baseline untouched; rejected work is disposable" if rollback_ok else "baseline could not be proven untouched", {"target": trusted_baseline.revision_id if trusted_baseline else None}))
        accepted = all(gate.passed for gate in gates)
        if accepted:
            outcome = "ACCEPTED_EVOLUTION" if evaluation_mode is EvaluationMode.EVOLUTION else "ACCEPTED_RECOVERY"
        else:
            outcome = "REJECTED_ROLLBACK"
        return self._new_receipt(
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=duration_sec,
            candidate=candidate_descriptor,
            baseline=trusted_baseline,
            mode=evaluation_mode,
            metrics=metrics,
            primary_dimension=primary,
            improvement=improvement,
            gates=gates,
            accepted=accepted,
            outcome=outcome,
            rollback_performed=not accepted,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            result_verified=result_verified,
            error=error,
            metadata=(
                parsed_result.get("metadata", {})
                if isinstance(parsed_result, Mapping)
                and isinstance(parsed_result.get("metadata", {}), Mapping)
                else {}
            ),
        )

    # Common vocabulary aliases make the seam convenient for orchestrators.
    run = evaluate
    assess = evaluate
    evaluate_candidate = evaluate
    evaluate_revision = evaluate


__all__ = [
    "HARNESS_SCHEMA_VERSION",
    "HARNESS_VERSION",
    "RESULT_PREFIX",
    "EMPTY_FIXTURE_HASH",
    "EMPTY_DIRECTORY_HASH",
    "EvaluationHarnessError",
    "HarnessConfigurationError",
    "CandidateIsolationError",
    "CandidateResultError",
    "EvaluationMode",
    "HardGate",
    "ResourceBudget",
    "FixtureEntry",
    "FixtureSet",
    "CandidateRevision",
    "BaselineRevision",
    "GateResult",
    "EvaluationReceipt",
    "EvaluationHarness",
    "directory_fingerprint",
]
