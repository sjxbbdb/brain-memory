"""Minimal external host for the P7 controlled-iteration rehearsal.

The host is deliberately outside the runtime ``BrainStem``.  It owns the
temporary active/candidate trees, the fixed fixture judge, model credentials,
and the explicit authorization boundary.  It never writes the source
checkout or a remote repository and it never treats a synthetic candidate as
an accepted iteration.

The module can be used as a small library or as a two-phase CLI::

    python tools/p7_controlled_host.py --prepare
    python tools/p7_controlled_host.py --authorize <run-id>
    python tools/p7_controlled_host.py --inspect-orphans
    python tools/p7_controlled_host.py --prepare-orphan-recovery
    python tools/p7_controlled_host.py --verify-orphan-recovery <recovery-id>

``--prepare`` leaves a bounded run directory in the OS temporary directory so
an operator can inspect the public receipt before authorizing the second
phase.  The directory contains the isolated runtime trees and no model key,
prompt, raw source evidence, or full evaluation receipt.

``--inspect-orphans`` performs an observation-only, non-atomic lease snapshot;
exit 0 means only that the snapshot was authenticated, never that P7 is ready.
Recovery preparation/verification records evidence only; verification requires
a full host restart and nonce-scoped Docker absence and never removes or rewrites the old lease.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
from contextlib import contextmanager
from dataclasses import dataclass, replace
import ctypes
import difflib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
from types import MappingProxyType
import uuid


# ``python tools/p7_controlled_host.py`` sets ``sys.path[0]`` to ``tools``
# rather than the repository root.  Resolve the local package root before any
# lazy ``brain.*`` import so the direct CLI and ``python -m`` entry points
# share the same fail-closed behavior.  This is only an import path adjustment
# and never becomes part of a public receipt.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


DEFAULT_TARGET_ID = "goal_determinism"
HOST_SCHEMA_VERSION = 1
RUN_DIRECTORY_PREFIX = "brain-memory-p7-"
DEFAULT_SEEDS = (1, 5, 8, 13, 21, 34, 55, 89)
DOCKER_IMAGE_ENV = "BRAIN_MEMORY_P7_DOCKER_IMAGE"
DOCKER_CLI_ENV = "BRAIN_MEMORY_P7_DOCKER_CLI"
MANIFEST_KEY_ENV = "BRAIN_MEMORY_P7_MANIFEST_KEY"
_SANDBOX_PROTOCOL = "p7-docker-v1"
_DOCKER_SECURITY_FLAGS = (
    "--pull=never",
    "--network=none",
    "--ipc=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=64",
    "--memory=256m",
    "--memory-swap=256m",
    "--cpus=1.0",
    "--log-driver=none",
    "--shm-size=16m",
    "--ulimit=nofile=256:256",
    "--ulimit=nproc=64:64",
    "--user=65534:65534",
    "--init",
)
_DOCKER_TMPFS_FLAG = "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777"
_DOCKER_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,220}@sha256:[0-9a-f]{64}$"
)
_DOCKER_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_FULL_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|credential|cookie|private[_-]?key|lease[_-]?token)(?:$|_)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/]|(?<![A-Za-z0-9_])/(?![/\s])[^\r\n\t<>\"']+)",
    re.IGNORECASE,
)
_URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9_]{12,}\b|\bsk-[A-Za-z0-9_-]{16,}\b|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)",
    re.IGNORECASE,
)
_MUTABLE_SUFFIXES = (
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
_COPY_MAX_FILES = 20_000
_COPY_MAX_BYTES = 256 * 1024 * 1024
_COPY_MAX_DEPTH = 64
_RUN_METADATA_MAX_BYTES = 1024 * 1024
_MANIFEST_MAX_BYTES = 16 * 1024
_DOCKER_CLI_MAX_BYTES = 256 * 1024 * 1024
_AUTHORIZATION_LOCK_TIMEOUT_SEC = 5.0
_MANIFEST_SCHEMA_VERSION = 1
_PRIVATE_FILE_RE = re.compile(
    r"(?:^|[._ -])(?:api[._ -]?key|apikey|secret|credential|credentials|password|passwd|token|private[._ -]?key)(?:$|[._ -])",
    re.IGNORECASE,
)
_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
    }
)
_PROTECTED_SCOPE_WORDS = frozenset(
    {
        "api",
        "agent_bridge.py",
        "brain_stem.py",
        "core.py",
        "evaluator",
        "evaluation_harness.py",
        "evolution.py",
        "life_kernel.py",
        "motivation.py",
        "succession",
        "storage",
        "services",
        "config.py",
        "version.py",
        "readme.md",
        "context.md",
    }
)


def _digest(value: bytes | str) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def _bounded_file_digest(path: Path | str, *, max_bytes: int = 256 * 1024) -> str:
    """Hash one host-owned fixture file without following aliases or reading unbounded data."""

    source = Path(path)
    if _is_link_like(source) or not source.is_file():
        raise ValueError("fixture file is unavailable")
    descriptor: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | int(getattr(os, "O_NOFOLLOW", 0))
            | int(getattr(os, "O_BINARY", 0))
        )
        descriptor = os.open(os.fspath(source), flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
            raise ValueError("fixture file is unsafe")
        if int(info.st_size) > max_bytes:
            raise ValueError("fixture file exceeds its bounded size")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(data) > max_bytes
            or info.st_size != after.st_size
            or info.st_mtime_ns != after.st_mtime_ns
            or getattr(info, "st_ino", 0) != getattr(after, "st_ino", 0)
        ):
            raise ValueError("fixture file changed while being read")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("fixture file is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _digest(bytes(data))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class IterationTarget:
    """One host-owned, low-risk target admitted to the P7 pipeline."""

    target_id: str
    scope: str
    protected: bool
    probe_kind: str
    candidate_kind: str

    def contract(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "scope": self.scope,
            "protected": self.protected,
            "probe_kind": self.probe_kind,
            "candidate_kind": self.candidate_kind,
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "scope": self.scope,
            "contract_digest": target_contract_digest(self),
        }


def target_contract_digest(target: IterationTarget) -> str:
    if not isinstance(target, IterationTarget):
        raise TypeError("target must be an IterationTarget")
    return _digest(_canonical(target.contract()))


ITERATION_TARGETS = MappingProxyType(
    {
        DEFAULT_TARGET_ID: IterationTarget(
            target_id=DEFAULT_TARGET_ID,
            scope="brain/drive_engine.py",
            protected=False,
            probe_kind="goal_generator_replay",
            candidate_kind="deterministic_hash_repair",
        )
    }
)


def resolve_iteration_target(target_id: str = DEFAULT_TARGET_ID) -> IterationTarget:
    """Resolve only a host-owned target id; paths are never caller-defined."""

    if not isinstance(target_id, str) or target_id not in ITERATION_TARGETS:
        raise ValueError("unknown iteration target")
    target = ITERATION_TARGETS[target_id]
    if target.protected or target.scope.startswith(("/", "\\")) or ":" in target.scope:
        raise ValueError("iteration target is not eligible")
    if ".." in Path(target.scope).parts:
        raise ValueError("iteration target escapes the repository")
    return target


TARGET_SCOPE = resolve_iteration_target().scope


def _is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows reparse points without following them.

    Checking parents closes the junction/reparse alias that would otherwise
    make a seemingly safe child resolve outside the host-owned tree.
    """

    try:
        for item in (path, *path.parents):
            if item.is_symlink():
                return True
            attributes = getattr(
                item.stat(follow_symlinks=False),
                "st_file_attributes",
                0,
            )
            if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                return True
        return False
    except OSError:
        return True


def _lexists(path: Path | str) -> bool:
    """Return whether a path entry exists without following a link."""

    try:
        return bool(os.path.lexists(os.fspath(path)))
    except (OSError, ValueError, TypeError):
        # An entry that cannot be inspected is not safe to create over.
        return True


def _assert_run_metadata_paths(root: Path) -> None:
    """Reject pre-created aliases for every mutable run sidecar."""

    for name in (
        "run.json",
        "run.private.json",
        "state.sqlite",
        "state.sqlite-wal",
        "state.sqlite-shm",
        "state.sqlite-journal",
        "promotion.jsonl",
        "promotion.jsonl.lock",
    ):
        child = root / name
        if _lexists(child) and (_is_link_like(child) or not child.is_file()):
            raise ValueError("P7 run metadata has an unsafe file")


def _coerce_manifest_key(value: Any) -> bytes:
    """Normalize a host-only manifest key without ever persisting it."""

    if isinstance(value, (bytes, bytearray)):
        key = bytes(value)
    else:
        text = str(value or "").strip()
        if text.lower().startswith("hex:"):
            try:
                key = bytes.fromhex(text[4:])
            except ValueError as exc:
                raise ValueError("P7 manifest key is invalid") from exc
        else:
            key = text.encode("utf-8")
    if len(key) < 16:
        raise ValueError("P7 manifest key is too short")
    return key


def _manifest_key_for_run(master_key: bytes, run_id: str) -> bytes:
    """Derive a distinct HMAC key for one run from the external host secret."""

    context = ("brain-memory-p7-manifest-v1:" + str(run_id)).encode("utf-8")
    return hmac.new(master_key, context, hashlib.sha256).digest()


@contextmanager
def _authorization_file_lock(
    path: Path | str, *, timeout_sec: float = _AUTHORIZATION_LOCK_TIMEOUT_SEC
):
    """Serialize authorization claims across host processes.

    The lock is only a coordination primitive; the HMAC manifest remains the
    trust boundary.  A sidecar is deliberately kept outside the run directory
    by ``RunLayout`` so a candidate tree cannot replace the coordination file.
    """

    lock_path = Path(path)
    if _lexists(lock_path) and _is_link_like(lock_path):
        raise RuntimeError("authorization lock path is unsafe")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    # O_NOFOLLOW closes the common POSIX symlink race while opening an existing
    # lock.  Windows uses the post-open reparse/link check below.
    if hasattr(os, "O_NOFOLLOW"):
        flags |= int(getattr(os, "O_NOFOLLOW"))
    stream = None
    fd = None
    try:
        try:
            fd = os.open(str(lock_path), flags, 0o600)
            stream = os.fdopen(fd, "a+b", buffering=0)
            fd = None
        except OSError as exc:
            raise RuntimeError("authorization lock unavailable") from exc
        try:
            opened = os.fstat(stream.fileno())
        except OSError as exc:
            raise RuntimeError("authorization lock unavailable") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_nlink", 1) > 1
            or _is_link_like(lock_path)
        ):
            raise RuntimeError("authorization lock path is unsafe")

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
                            raise RuntimeError("authorization lock timeout")
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
                            raise RuntimeError("authorization lock timeout")
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
                    pass
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def _diagnostic_summary(value: Any, limit: int = 240) -> str:
    """Return a bounded error summary without paths, URIs, or token-shaped secrets."""
    text = str(value or "").replace("\x00", "").strip()
    text = _SECRET_VALUE_RE.sub("[REDACTED]", text)
    text = _URI_RE.sub("[REDACTED]", text)
    text = _ABSOLUTE_PATH_RE.sub("[REDACTED]", text)
    return text[:limit]


def _normalized_container_contract(contract: Mapping[str, Any]) -> dict[str, bool]:
    """Digest only the stable, path/ID-free container contract dimensions."""
    return {
        key: contract.get(key) is True
        for key in (
            "network", "filesystem", "privilege", "resources",
            "image", "identity", "ownership",
        )
    }


def _safe_child_environment(*, root: Path, hash_seed: int | None = None) -> dict[str, str]:
    """Build a minimal child environment; credentials never reach judges."""

    allowed = {"PATH", "Path", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    python_path = str(root)
    env["PYTHONPATH"] = python_path
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["BRAIN_MEMORY_OFFLINE"] = "1"
    if hash_seed is not None:
        env["PYTHONHASHSEED"] = str(int(hash_seed))
    return env


def _safe_docker_environment() -> dict[str, str]:
    """Use only the local default daemon; never forward credentials or proxies."""

    allowed = {
        "PATH", "Path", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT",
        # Docker Desktop resolves contexts from the user's configuration on
        # Windows.  These location hints are needed for context inspection;
        # credentials and proxy variables remain excluded.
        "USERPROFILE", "APPDATA", "DOCKER_CONFIG",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


@dataclass(frozen=True)
class _BoundedCommandResult:
    """Small, bounded result envelope for host-owned child processes."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    overflow: bool
    timed_out: bool
    drained: bool


def _bounded_finished(result: _BoundedCommandResult) -> bool:
    """Return true only when a bounded command completed without uncertainty.

    A process can exit with code zero at the same instant the deadline is
    reached.  ``returncode == 0`` is therefore not sufficient evidence that
    the command completed within its budget.  The reader state is part of the
    protocol too: a partial pipe read must never be treated as an attested
    response.
    """

    return bool(
        result.returncode is not None
        and not result.timed_out
        and not result.overflow
        and result.drained
    )


def _bounded_process_stopped(result: _BoundedCommandResult) -> bool:
    """Return whether the process/pipe boundary reached a known terminal state.

    This intentionally differs from :func:`_bounded_finished`: a timed-out or
    over-limit command can still have been stopped and drained, which is useful
    for durable lease recovery, but its output is never accepted as a command
    result or as same-invocation destructive-cleanup evidence.
    """

    return bool(result.returncode is not None and result.drained)


def _bounded_success(result: _BoundedCommandResult) -> bool:
    """Return true only for a complete, bounded command with exit code zero."""

    return bool(result.returncode == 0 and _bounded_finished(result))


def _create_kill_job(process: subprocess.Popen[Any]) -> Any:
    """Attach a Windows child to a kill-on-close Job Object when available.

    ``CREATE_NEW_PROCESS_GROUP`` is not enough on Windows: a child can exit
    while a grandchild keeps the inherited stdout handle open.  A Job Object
    gives the host a kernel-enforced tree boundary.  POSIX callers use a
    process group instead, so this helper is a no-op there.
    """

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
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )]

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
        set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
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
            # Ownership of the handle moves to the returned job tuple.
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
    """Resume a Windows child only after it has entered the Job Object."""

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
    """Terminate a process and descendants without a shell."""

    boundary_closed = True
    if job:
        # Closing a kill-on-close Job Object is the authoritative Windows tree
        # termination mechanism; taskkill remains a best-effort fallback.
        boundary_closed = _close_kill_job(job)
        if not boundary_closed and os.name == "nt":
            try:
                system_root = os.environ.get("SystemRoot", r"C:\\Windows")
                taskkill = Path(system_root) / "System32" / "taskkill.exe"
                if taskkill.is_file() and not _is_link_like(taskkill):
                    taskkill_result = subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        close_fds=True,
                        timeout=3,
                        check=False,
                    )
                    if getattr(taskkill_result, "returncode", None) is None:
                        boundary_closed = False
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    elif os.name != "nt":
        try:
            os.killpg(process_group or process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group is already gone; this is a successful terminal state.
            pass
        except (AttributeError, OSError):
            # EPERM/EACCES (or an unavailable killpg primitive) means the
            # process boundary was not proven closed.  Do not let a readable
            # pipe turn that uncertainty into an attested completion.
            boundary_closed = False
    else:
        # Without the kill-on-close Job Object, taskkill is best effort only;
        # do not report a proven descendant boundary even if it returns zero.
        boundary_closed = False
        try:
            system_root = os.environ.get("SystemRoot", r"C:\\Windows")
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            if taskkill.is_file() and not _is_link_like(taskkill):
                taskkill_result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    close_fds=True,
                    timeout=3,
                    check=False,
                )
                if getattr(taskkill_result, "returncode", None) is None:
                    boundary_closed = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        # The parent is already gone; the group result above remains
        # authoritative for descendants.
        pass
    except (AttributeError, OSError, ValueError):
        boundary_closed = False
    return boundary_closed


def _run_bounded_command(
    argv: Sequence[str],
    *,
    timeout: float,
    limit: int = 65536,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    capture_stderr: bool = False,
) -> _BoundedCommandResult:
    """Run one external command with bounded output and tree cleanup.

    The reader drains continuously to avoid pipe deadlocks.  Overflow and
    timeout both terminate the complete process tree; normal exit also closes
    the tree boundary so descendants cannot retain a pipe or a mounted tree.
    """

    if limit < 0 or timeout <= 0:
        raise ValueError("bounded command limits are invalid")
    try:
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if env is not None:
            options["env"] = dict(env)
        if cwd is not None:
            options["cwd"] = str(cwd)
        if os.name == "nt":
            options["creationflags"] = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ) | 0x00000004  # CREATE_SUSPENDED; assign the Job first
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(list(argv), **options)
    except (OSError, ValueError):
        # No process handle means there is no observable process/tree or pipe
        # boundary.  Keep ``drained`` false so callers cannot treat a spawn
        # failure as proof that a side-effecting command stopped.
        return _BoundedCommandResult(None, b"", b"", False, False, False)

    stdout = bytearray()
    stderr = bytearray()
    overflow = False
    drained = True
    boundary_closed = True
    stop_event = threading.Event()
    process_group = process.pid if os.name != "nt" else None
    job: Any = None
    readers: list[threading.Thread] = []

    def terminate_safely() -> bool:
        """Attempt tree termination without masking the caller's exception."""

        nonlocal job
        current_job = job
        # A Job handle is single-use here: once termination is attempted, do
        # not let a later cleanup path close/reuse the same kernel handle.
        job = None
        try:
            return bool(
                _terminate_process_tree(
                    process, job=current_job, process_group=process_group
                )
            )
        except BaseException:
            return False

    def abort_setup() -> _BoundedCommandResult:
        """Close a partially-created process before returning an unknown result."""

        nonlocal boundary_closed, drained
        boundary_closed = terminate_safely() and boundary_closed
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            boundary_closed = terminate_safely() and boundary_closed
            try:
                process.wait(timeout=1)
            except BaseException:
                boundary_closed = False
        except BaseException:
            boundary_closed = False
            # A wait interruption must not skip the final kill attempt.
            boundary_closed = terminate_safely() and boundary_closed
        for stream in (process.stdout, process.stderr if capture_stderr else None):
            try:
                if stream is not None:
                    stream.close()
            except BaseException:
                drained = False
        for reader in readers:
            try:
                reader.join(timeout=1)
            except BaseException:
                drained = False
        return _BoundedCommandResult(None, b"", b"", False, False, False)

    def drain(stream: Any, buffer: bytearray) -> None:
        nonlocal overflow, drained
        if stream is None:
            drained = False
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if len(buffer) < limit + 1:
                    buffer.extend(chunk[: limit + 1 - len(buffer)])
                if len(buffer) > limit:
                    overflow = True
                    stop_event.set()
        except Exception:
            drained = False
        except BaseException:
            # A reader that is interrupted is not evidence that the pipe (or
            # its child process) reached a known terminal state.  Keep the
            # result fail-closed even though the exception occurs on a daemon
            # thread.
            drained = False
        finally:
            try:
                stream.close()
            except BaseException:
                drained = False

    try:
        job = _create_kill_job(process)
        if os.name == "nt" and not job:
            # A Windows process must not run outside a kernel-enforced tree
            # boundary.  The taskkill fallback is cleanup-only and is not a
            # sufficient isolation guarantee for a new execution.
            return abort_setup()
        if not _resume_suspended_process(process):
            return abort_setup()
        readers = [
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
        ]
        if capture_stderr:
            readers.append(
                threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
            )
        for reader in readers:
            reader.start()
    except BaseException:
        # Setup itself is part of the process boundary.  If attaching the Job,
        # resuming, or starting a reader fails, close every known handle before
        # preserving the original exception.
        abort_setup()
        raise

    timed_out = False
    cleanup_error: BaseException | None = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if stop_event.is_set():
                boundary_closed = terminate_safely() and boundary_closed
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                boundary_closed = terminate_safely() and boundary_closed
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # Interrupts and other asynchronous exceptions must still close the
        # creator's process boundary before they are allowed to propagate.
        boundary_closed = terminate_safely() and boundary_closed
        raise
    finally:
        # The parent can exit while descendants still own stdout.  Always
        # close the process-group/job boundary on the normal path as well.
        try:
            process_exited = process.poll() is not None
        except BaseException as exc:
            process_exited = False
            boundary_closed = False
            cleanup_error = cleanup_error or exc
        if process_exited:
            boundary_closed = terminate_safely() and boundary_closed
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out = True
            boundary_closed = terminate_safely() and boundary_closed
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                boundary_closed = False
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
        except BaseException as exc:
            boundary_closed = False
            cleanup_error = cleanup_error or exc
            boundary_closed = terminate_safely() and boundary_closed
        for reader in readers:
            try:
                reader.join(timeout=3)
            except BaseException as exc:
                drained = False
                cleanup_error = cleanup_error or exc
            try:
                reader_alive = reader.is_alive()
            except BaseException as exc:
                reader_alive = True
                drained = False
                cleanup_error = cleanup_error or exc
            if reader_alive:
                drained = False
                try:
                    if reader is readers[0] and process.stdout is not None:
                        process.stdout.close()
                    elif capture_stderr and process.stderr is not None:
                        process.stderr.close()
                except BaseException:
                    drained = False
                try:
                    reader.join(timeout=1)
                except BaseException as exc:
                    drained = False
                    cleanup_error = cleanup_error or exc
        if job:
            try:
                boundary_closed = _close_kill_job(job) and boundary_closed
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
            job = None
    try:
        readers_finished = all(not reader.is_alive() for reader in readers)
    except BaseException as exc:
        readers_finished = False
        drained = False
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error
    return _BoundedCommandResult(
        process.returncode,
        bytes(stdout),
        bytes(stderr),
        overflow,
        timed_out,
        drained and boundary_closed and readers_finished,
    )


def _validate_docker_cli(value: Any) -> str:
    """Accept only one explicit, real Docker executable path.

    The sandbox attestation must not depend on a repository-controlled PATH.
    A custom installation is therefore an operator-owned configuration value;
    common system locations are considered separately by
    :func:`_configured_docker_cli`.
    """

    rendered = _safe_text(value, 1000)
    if not rendered or any(character in rendered for character in ("\r", "\n")):
        return ""
    source = Path(rendered)
    if not source.is_absolute() or source.name.lower() not in {"docker", "docker.exe"}:
        return ""
    try:
        if _is_link_like(source) or not source.is_file():
            return ""
        resolved = source.resolve(strict=True)
        # This also rejects an alias introduced by a symlink/junction parent.
        if resolved != source:
            return ""
        info = source.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_nlink", 1) > 1
            or int(info.st_size) > _DOCKER_CLI_MAX_BYTES
            or (os.name != "nt" and not os.access(source, os.X_OK))
        ):
            return ""
    except (OSError, ValueError):
        return ""
    return str(resolved)


def _configured_docker_cli(value: str | None = None) -> str:
    """Resolve Docker without searching PATH.

    ``BRAIN_MEMORY_P7_DOCKER_CLI`` is the trust root for custom installs.  If
    it is absent, only fixed platform installation locations are considered.
    """

    if value is not None:
        return _validate_docker_cli(value)
    configured = os.environ.get(DOCKER_CLI_ENV)
    if configured is not None:
        return _validate_docker_cli(configured)
    candidates = (
        (
            r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        )
        if os.name == "nt"
        else (
            "/usr/bin/docker",
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
        )
    )
    for candidate in candidates:
        trusted = _validate_docker_cli(candidate)
        if trusted:
            return trusted
    return ""


def _configured_docker_context(docker: str | None = None) -> str:
    executable = _validate_docker_cli(docker) if docker else _configured_docker_cli()
    if not executable:
        return ""
    result = _run_bounded_command(
        [executable, "context", "show"],
        env=_safe_docker_environment(),
        timeout=3,
        limit=4096,
    )
    context = result.stdout.decode("utf-8", errors="ignore").strip()
    return context if (
        result.returncode == 0
        and not result.timed_out
        and not result.overflow
        and result.drained
        and _DOCKER_CONTEXT_RE.fullmatch(context)
    ) else ""


def _local_docker_context_endpoint(docker: str, context: str) -> str:
    if not _DOCKER_CONTEXT_RE.fullmatch(context):
        return ""
    result = _run_bounded_command(
        [
            docker,
            "context",
            "inspect",
            "--format={{json .Endpoints.docker.Host}}",
            context,
        ],
        env=_safe_docker_environment(),
        timeout=3,
        limit=4096,
    )
    if not _bounded_success(result):
        return ""
    try:
        endpoint = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError):
        return ""
    if not isinstance(endpoint, str):
        return ""
    lowered = endpoint.strip().lower()
    return endpoint.strip() if lowered.startswith(("npipe://", "unix://")) else ""


def _configured_docker_image(value: str | None = None) -> str:
    selected = os.environ.get(DOCKER_IMAGE_ENV, "") if value is None else value
    return _safe_text(selected, 300)


def _docker_mount(source: Path, destination: str) -> str:
    source = Path(source).expanduser()
    # Resolve and validate the exact directory before rendering a Docker CLI
    # argument.  A later caller must not be able to smuggle a symlink/junction
    # (or a non-directory file) into the bind surface after the initial copy.
    if _is_link_like(source) or not source.is_dir():
        raise ValueError("sandbox mount source is unsafe")
    resolved = source.resolve(strict=True)
    if resolved != source:
        raise ValueError("sandbox mount source is aliased")
    rendered = str(resolved)
    if any(character in rendered for character in ("\x00", "\r", "\n", ",")):
        raise ValueError("sandbox mount path is not representable")
    return f"--mount=type=bind,src={rendered},dst={destination},readonly"


_AUTO_DOCKER_MOUNT_DESTINATIONS = frozenset(
    {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
)


def _tmpfs_options_are_exact(value: Any) -> bool:
    """Accept only the tmpfs options emitted by ``_DOCKER_TMPFS_FLAG``."""

    if not isinstance(value, str):
        return False
    options = {
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    }
    required = {"rw", "noexec", "nosuid", "nodev"}
    size = next((item for item in options if item.startswith("size=")), "")
    # Docker may render the same 32 MiB size as ``32m`` or bytes.
    size_value = size.removeprefix("size=")
    try:
        if size_value.endswith("m"):
            size_bytes = int(size_value[:-1]) * 1024 * 1024
        else:
            size_bytes = int(size_value)
    except (TypeError, ValueError):
        return False
    allowed = required | {size, "mode=1777"}
    return (
        required <= options
        and size
        and size_bytes == 32 * 1024 * 1024
        and options <= allowed
    )


def _bind_spec_matches(spec: Any, source: Path, destination: str) -> bool:
    """Match a Docker ``HostConfig.Binds`` entry without ambiguous splitting."""

    if not isinstance(spec, str):
        return False
    # ``source`` may contain a Windows drive colon; strip the destination and
    # read-only suffix from the right instead of splitting on every colon.
    suffixes = (f":{destination}:ro", f":{destination}:ro,rprivate")
    suffix = next((item for item in suffixes if spec.endswith(item)), "")
    if not suffix:
        return False
    rendered_source = spec[: -len(suffix)]
    if re.fullmatch(r"(?:\\\\\\?\\)?[A-Za-z]:[\\/].+", rendered_source):
        expected_windows = str(source).replace("/", "\\\\").rstrip("\\\\").casefold().removeprefix("\\\\?\\\\")
        actual_windows = rendered_source.replace("/", "\\\\").rstrip("\\\\").casefold().removeprefix("\\\\?\\\\")
        return actual_windows == expected_windows and not _is_link_like(Path(rendered_source))
    try:
        expected = Path(source).expanduser().resolve(strict=True)
        # Docker Desktop may echo Linux-engine spelling for a Windows bind.
        mapped = re.fullmatch(
            r"(?:/host_mnt|/run/desktop/mnt/host|/mnt/host)/([A-Za-z])/(.+)",
            rendered_source,
        )
        if mapped:
            rendered_source = mapped.group(1).upper() + ":\\\\" + mapped.group(2).replace("/", "\\\\")
        actual = Path(rendered_source).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return False
    actual_key = str(actual).replace("/", "\\").rstrip("\\").casefold()
    expected_key = str(expected).replace("/", "\\").rstrip("\\").casefold()
    actual_key = actual_key.removeprefix("\\\\?\\")
    expected_key = expected_key.removeprefix("\\\\?\\")
    return actual_key == expected_key and not _is_link_like(Path(rendered_source))


def _inspect_mount_contract(
    mounts: Any,
    host: Mapping[str, Any],
    expected_sources: Mapping[str, Path],
) -> bool:
    """Verify the exact writable surface and reject hidden host capabilities."""

    if not isinstance(mounts, list) or not isinstance(host, Mapping):
        return False
    # The explicit bind list is authoritative for host-side source paths.  A
    # missing list is uncertainty, not permission to fall back to a partial
    # ``Mounts`` check.
    binds = host.get("Binds")
    if binds is not None and (not isinstance(binds, list) or len(binds) != len(expected_sources)):
        return False
    if isinstance(binds, list):
        for destination, source in expected_sources.items():
            if not any(_bind_spec_matches(item, source, destination) for item in binds):
                return False

    seen: set[str] = set()
    allowed = set(expected_sources) | _AUTO_DOCKER_MOUNT_DESTINATIONS | {"/tmp"}
    for item in mounts:
        if not isinstance(item, Mapping):
            return False
        destination = item.get("Destination")
        if not isinstance(destination, str) or destination in seen or destination not in allowed:
            return False
        seen.add(destination)
        if destination in expected_sources:
            if item.get("Type") != "bind" or item.get("RW") is not False:
                return False
            source_text = item.get("Source")
            if not isinstance(source_text, str) or not source_text:
                return False
            try:
                expected_source = Path(expected_sources[destination]).resolve(strict=True)
                actual_path = source_text
                windows_source = bool(re.fullmatch(r"(?:\\\\\\?\\)?[A-Za-z]:[\\/].+", source_text))
                if windows_source:
                    # When the Linux daemon returns a native Windows source,
                    # do not pass it through the host OS Path resolver (which
                    # would interpret it as a relative POSIX path). Compare
                    # the lossless Windows spellings directly instead.
                    expected_windows = str(expected_sources[destination]).replace("/", "\\").rstrip("\\").casefold()
                    actual_windows = source_text.replace("/", "\\").rstrip("\\").casefold()
                    expected_windows = expected_windows.removeprefix("\\\\?\\")
                    actual_windows = actual_windows.removeprefix("\\\\?\\")
                    if actual_windows != expected_windows:
                        return False
                    continue
                # Docker Desktop's Linux engine reports Windows bind sources
                # as /host_mnt/<drive>/...; normalize only that explicit,
                # lossless form before comparing the real directory.
                match = re.fullmatch(
                    r"(?:/host_mnt|/run/desktop/mnt/host|/mnt/host)/([A-Za-z])/(.+)",
                    source_text,
                )
                if match:
                    actual_path = match.group(1).upper() + ":\\" + match.group(2).replace("/", "\\")
                else:
                    # Docker Desktop may return the Windows form directly
                    # (``D:\\brain-memory``) even though the daemon is Linux.
                    # Keep this as an explicit lossless representation rather
                    # than accepting arbitrary path aliases.
                    windows_path = re.fullmatch(r"([A-Za-z]):[\\/](.+)", source_text)
                    if windows_path:
                        actual_path = windows_path.group(1).upper() + ":\\" + windows_path.group(2).replace("/", "\\")
                actual_source = Path(actual_path).expanduser().resolve(strict=True)
            except (OSError, ValueError):
                return False
            canonical_actual = str(actual_source).replace("/", "\\").rstrip("\\").casefold()
            canonical_expected = str(expected_source).replace("/", "\\").rstrip("\\").casefold()
            canonical_actual = canonical_actual.removeprefix("\\\\?\\")
            canonical_expected = canonical_expected.removeprefix("\\\\?\\")
            if actual_source != expected_source and canonical_actual != canonical_expected:
                try:
                    if not os.path.samefile(actual_source, expected_source):
                        return False
                except (OSError, ValueError):
                    return False
        elif destination == "/tmp":
            if item.get("Type") != "tmpfs" or item.get("RW") is not True:
                return False
            if item.get("Source") not in ("", None):
                return False
        else:
            # Docker Desktop can add these three read-only bookkeeping files;
            # no other daemon-provided host mount is accepted.
            if item.get("Type") != "bind" or item.get("RW") is not False:
                return False

    if set(expected_sources) - seen:
        return False

    # The tmpfs is represented in HostConfig even on engines that omit it from
    # Mounts.  It must be the sole tmpfs entry and carry the exact options.
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, Mapping) or set(tmpfs) != {"/tmp"}:
        return False
    if not _tmpfs_options_are_exact(tmpfs.get("/tmp")):
        return False

    # Reject capability-bearing alternatives that are not part of this
    # protocol.  Missing optional fields are treated as their Docker-safe
    # defaults; non-empty values fail closed.
    if host.get("Privileged", False) is not False:
        return False
    for key in ("CapAdd", "Devices", "DeviceRequests", "VolumesFrom", "Links", "PortBindings"):
        if host.get(key) not in (None, [], {}):
            return False
    if host.get("AutoRemove", False) not in (False, None):
        return False
    if host.get("RestartPolicy") not in (
        None,
        {},
        {"Name": "", "MaximumRetryCount": 0},
        {"Name": "no", "MaximumRetryCount": 0},
    ):
        return False
    if host.get("PidMode") not in (None, "") or host.get("UTSMode") not in (None, ""):
        return False
    if host.get("UsernsMode") not in (None, ""):
        return False
    if host.get("CgroupnsMode") not in (None, "", "private"):
        return False
    security_options = {
        str(item).strip().lower() for item in host.get("SecurityOpt", [])
    }
    if security_options not in ({"no-new-privileges"}, {"no-new-privileges:true"}):
        return False
    return True


def _sandbox_capability_report(
    image_ref: str | None = None,
    context_name: str | None = None,
    *,
    docker_cli: str | None = None,
    lease_store: OrphanLeaseStore | None = None,
) -> dict[str, Any]:
    """Verify the exact local Docker boundary used by the fixture judge."""

    docker: str | None = None
    context = ""
    endpoint = ""

    def blocked(
        reason: str,
        *,
        daemon: bool = False,
        image_pinned: bool = False,
        image_available: bool = False,
        contract_checks: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        report = {
            "ready": False,
            "executor": "docker" if docker else "none",
            "protocol": _SANDBOX_PROTOCOL,
            "local_context_verified": bool(endpoint),
            "daemon_reachable": daemon,
            "image_pinned": image_pinned,
            "image_available": image_available,
            "probe_verified": False,
            "cleanup_verified": False,
            "network_isolation_verified": False,
            "filesystem_isolation_verified": False,
            "privilege_isolation_verified": False,
            "resource_limits_configured": False,
            "reason": reason,
        }
        if contract_checks is not None:
            # Public diagnostics are deliberately boolean-only: never expose
            # Docker's raw inspect payload, paths, IDs, or image references.
            checks: dict[str, Any] = {}
            for name, value in contract_checks.items():
                key = str(name)
                if key == "mount_count" and isinstance(value, int) and not isinstance(value, bool):
                    checks[key] = value
                elif key == "probe_source_style" and value in {"windows", "host_mnt", "desktop_host", "other"}:
                    checks[key] = value
                elif key == "mount_destinations_safe" and isinstance(value, list):
                    checks[key] = [
                        {"/probe": "probe", "/tmp": "tmpfs", "/etc/hostname": "hostname",
                         "/etc/hosts": "hosts", "/etc/resolv.conf": "resolv"}.get(item, "unknown")
                        for item in value
                    ]
                else:
                    checks[key] = bool(value)
            report["contract_checks"] = checks
        return report

    docker = _configured_docker_cli(docker_cli)
    if not docker:
        return blocked("docker_cli_unavailable")
    try:
        docker_cli_digest = _bounded_file_digest(
            docker, max_bytes=_DOCKER_CLI_MAX_BYTES
        )
    except (OSError, ValueError):
        return blocked("docker_cli_unavailable")
    if not re.fullmatch(r"[0-9a-f]{64}", docker_cli_digest):
        return blocked("docker_cli_unavailable")
    context = _safe_text(
        context_name if context_name is not None else _configured_docker_context(docker),
        81,
    )
    endpoint = _local_docker_context_endpoint(docker, context)
    if not endpoint:
        return blocked("docker_context_not_local")
    endpoint_digest = _digest(endpoint)
    docker_command = [docker, "--context", context]
    daemon_result = _run_bounded_command(
        [*docker_command, "version", "--format={{.Server.Version}}"],
        env=_safe_docker_environment(),
        timeout=3,
        limit=4096,
    )
    if (
        not _bounded_success(daemon_result)
        or not daemon_result.stdout.decode("utf-8", errors="ignore").strip()
    ):
        return blocked("docker_daemon_unavailable")

    image = _configured_docker_image(image_ref)
    if not _DOCKER_IMAGE_RE.fullmatch(image):
        return blocked("docker_image_not_pinned", daemon=True)
    image_result = _run_bounded_command(
        [
            *docker_command,
            "image",
            "inspect",
            "--format={{.Id}}|{{json .RepoDigests}}",
            image,
        ],
        env=_safe_docker_environment(),
        timeout=5,
        limit=65536,
    )
    if (
        not _bounded_success(image_result)
    ):
        return blocked("docker_image_unavailable", daemon=True, image_pinned=True)
    try:
        image_id_text, repo_digests_text = image_result.stdout.decode(
            "utf-8", errors="strict"
        ).strip().split("|", 1)
        image_id = image_id_text.strip().lower()
        repo_digests = json.loads(repo_digests_text)
    except (UnicodeError, ValueError):
        return blocked("docker_image_unavailable", daemon=True, image_pinned=True)
    requested_digest = image.rsplit("@sha256:", 1)[1]
    digest_bound = bool(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        and isinstance(repo_digests, list)
        and any(
            isinstance(value, str)
            and value.lower().endswith("@sha256:" + requested_digest)
            for value in repo_digests
        )
    )
    if not digest_bound:
        return blocked("docker_image_digest_mismatch", daemon=True, image_pinned=True)

    probe_source = '''import json
import os
from pathlib import Path

def denied(path):
    try:
        Path(path).write_text("denied", encoding="utf-8")
    except OSError:
        return True
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
    return False

status = {}
try:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
except OSError:
    pass
try:
    interfaces = {entry.name for entry in Path("/sys/class/net").iterdir()}
except OSError:
    interfaces = set()
temporary = Path("/tmp/p7-probe.tmp")
try:
    temporary.write_text("ok", encoding="utf-8")
    temporary_write_verified = temporary.read_text(encoding="utf-8") == "ok"
finally:
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
network = bool(interfaces) and interfaces <= {"lo"}
filesystem = denied("/p7-root-write-test") and denied("/probe/p7-mount-write-test")
privilege = (
    hasattr(os, "geteuid")
    and os.geteuid() != 0
    and status.get("NoNewPrivs") == "1"
    and int(status.get("CapEff", "1"), 16) == 0
)
ready = network and filesystem and privilege and temporary_write_verified
print(json.dumps({
    "schema_version": 1,
    "ready": ready,
    "network_isolation_verified": network,
    "filesystem_isolation_verified": filesystem,
    "privilege_isolation_verified": privilege,
    "temporary_write_verified": temporary_write_verified,
}, sort_keys=True))
raise SystemExit(0 if ready else 3)
'''
    container_id = ""
    probe_nonce = secrets.token_hex(16)
    container_name = "brain-memory-p7-probe-" + probe_nonce
    probe_result: _BoundedCommandResult | None = None
    create_result: _BoundedCommandResult | None = None
    inspect_result: _BoundedCommandResult | None = None
    ownership_check: _BoundedCommandResult | None = None
    cleanup_result: _BoundedCommandResult | None = None
    post_cleanup: _BoundedCommandResult | None = None
    post_cleanup2: _BoundedCommandResult | None = None
    external_uncertain = False

    def command_boundary_proven() -> bool:
        """Require every started CLI to finish cleanly before same-run rm."""

        if external_uncertain:
            return False
        return all(
            result is None
            or (result.returncode is not None and _bounded_finished(result))
            for result in (
                create_result,
                inspect_result,
                probe_result,
                ownership_check,
                cleanup_result,
                post_cleanup,
                post_cleanup2,
            )
        )

    def endpoint_boundary_proven() -> bool:
        """Re-pin the local endpoint immediately before destructive cleanup."""

        try:
            if (
                _bounded_file_digest(docker, max_bytes=_DOCKER_CLI_MAX_BYTES)
                != docker_cli_digest
            ):
                return False
        except (OSError, ValueError):
            return False
        current_endpoint = _local_docker_context_endpoint(docker, context)
        return bool(current_endpoint and _digest(current_endpoint) == endpoint_digest)

    def docker_cli_boundary_proven() -> bool:
        try:
            return (
                _bounded_file_digest(docker, max_bytes=_DOCKER_CLI_MAX_BYTES)
                == docker_cli_digest
            )
        except (OSError, ValueError):
            return False
    inspected_contract: dict[str, Any] = {}
    failure_reason = ""
    cleanup_verified = False
    container_owned = False
    lease: OrphanLeaseHandle | None = None
    if lease_store is not None:
        try:
            lease = lease_store.arm(
                operation="probe",
                nonce=probe_nonce,
                name=container_name,
                image=image,
                context=context,
                docker_cli=docker,
                endpoint_digest=_digest(endpoint),
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return blocked(
                "orphan_lease_unavailable",
                daemon=True,
                image_pinned=True,
                image_available=True,
            )
    try:
        probe_parent = Path.cwd() / ".p7-sandbox-tmp"
        probe_parent.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="brain-memory-p7-sandbox-probe-", dir=str(probe_parent)
        ) as temp:
            probe_root = Path(temp)
            (probe_root / "probe.py").write_text(
                probe_source, encoding="utf-8", newline="\n"
            )
            create_result = _run_bounded_command(
                [
                    *docker_command,
                    "create",
                    *_DOCKER_SECURITY_FLAGS,
                    "--name=" + container_name,
                    "--label=brain-memory.p7.nonce=" + probe_nonce,
                    "--workdir=/tmp",
                    _DOCKER_TMPFS_FLAG,
                    _docker_mount(probe_root, "/probe"),
                    "--entrypoint=python",
                    image,
                    "/probe/probe.py",
                ],
                env=_safe_docker_environment(),
                timeout=10,
                limit=4096,
            )
            created_id = create_result.stdout.decode("ascii", errors="ignore").strip()
            if create_result.timed_out:
                failure_reason = "sandbox_probe_failed"
            elif (
                _bounded_success(create_result)
                and _FULL_CONTAINER_ID_RE.fullmatch(created_id)
            ):
                container_id = created_id
            else:
                failure_reason = "sandbox_probe_create_failed"
            if not failure_reason:
                inspect_result = _run_bounded_command(
                    [
                        *docker_command,
                        "container",
                        "inspect",
                        "--format={{json .}}",
                        container_id,
                    ],
                    env=_safe_docker_environment(),
                    timeout=5,
                    limit=262144,
                )
                if (
                    not _bounded_success(inspect_result)
                ):
                    failure_reason = "sandbox_config_unverified"
                else:
                    try:
                        inspected = json.loads(inspect_result.stdout.decode("utf-8", errors="strict"))
                    except (UnicodeError, ValueError):
                        inspected = None
                    config = inspected.get("Config", {}) if isinstance(inspected, Mapping) else {}
                    host = inspected.get("HostConfig", {}) if isinstance(inspected, Mapping) else {}
                    mounts = inspected.get("Mounts", []) if isinstance(inspected, Mapping) else []
                    inspected_container_id = (
                        str(inspected.get("Id", "")).lower()
                        if isinstance(inspected, Mapping)
                        else ""
                    )
                    inspected_container_name = (
                        str(inspected.get("Name", ""))
                        if isinstance(inspected, Mapping)
                        else ""
                    )
                    ulimits = {
                        item.get("Name"): (item.get("Soft"), item.get("Hard"))
                        for item in host.get("Ulimits", [])
                        if isinstance(item, Mapping)
                    } if isinstance(host, Mapping) else {}
                    tmpfs = host.get("Tmpfs", {}).get("/tmp", "") if isinstance(host, Mapping) and isinstance(host.get("Tmpfs"), Mapping) else ""
                    mount_verified = _inspect_mount_contract(
                        mounts,
                        host,
                        {"/probe": probe_root},
                    )
                    network_configured = bool(
                        isinstance(host, Mapping)
                        and host.get("NetworkMode") == "none"
                        and host.get("IpcMode") == "none"
                    )
                    filesystem_configured = bool(
                        isinstance(host, Mapping)
                        and host.get("ReadonlyRootfs") is True
                        and mount_verified
                        and isinstance(tmpfs, str)
                        and _tmpfs_options_are_exact(tmpfs)
                    )
                    filesystem_mount = bool(mount_verified)
                    filesystem_rootfs = bool(
                        isinstance(host, Mapping) and host.get("ReadonlyRootfs") is True
                    )
                    filesystem_tmpfs = bool(
                        isinstance(tmpfs, str) and _tmpfs_options_are_exact(tmpfs)
                    )
                    privilege_configured = bool(
                        isinstance(config, Mapping)
                        and config.get("User") == "65534:65534"
                        and isinstance(host, Mapping)
                        and "ALL" in {str(value).upper() for value in host.get("CapDrop", [])}
                        and any(
                            str(value).lower().startswith("no-new-privileges")
                            for value in host.get("SecurityOpt", [])
                        )
                    )
                    resources_configured = bool(
                        isinstance(host, Mapping)
                        and host.get("PidsLimit") == 64
                        and host.get("Memory") == 256 * 1024 * 1024
                        and host.get("MemorySwap") == 256 * 1024 * 1024
                        and host.get("NanoCpus") == 1_000_000_000
                        and isinstance(host.get("LogConfig"), Mapping)
                        and host.get("LogConfig", {}).get("Type") == "none"
                        and host.get("ShmSize") == 16 * 1024 * 1024
                        and host.get("Init") is True
                        and ulimits.get("nofile") == (256, 256)
                        and ulimits.get("nproc") == (64, 64)
                    )
                    image_configured = bool(
                        isinstance(config, Mapping)
                        and config.get("Image") in {image, image_id}
                        and isinstance(inspected, Mapping)
                        and str(inspected.get("Image", "")).lower() == image_id
                    )
                    identity_configured = bool(
                        _FULL_CONTAINER_ID_RE.fullmatch(inspected_container_id)
                        and inspected_container_name
                        in {container_name, "/" + container_name}
                        and (
                            inspected_container_id == created_id.lower()
                        )
                    )
                    ownership_configured = bool(
                        identity_configured
                        and
                        isinstance(config, Mapping)
                        and isinstance(config.get("Labels"), Mapping)
                        and config.get("Labels", {}).get("brain-memory.p7.nonce")
                        == probe_nonce
                        and config.get("Image") in {image, image_id}
                    )
                    # A create response is only an identifier, not proof of
                    # ownership.  Cleanup may use it only after the daemon's
                    # inspected label matches this invocation's nonce.
                    container_owned = ownership_configured
                    if container_owned:
                        # Destructive operations accept only Docker's full,
                        # no-trunc immutable identifier obtained from inspect.
                        container_id = inspected_container_id
                    inspected_contract = {
                        "network": network_configured,
                        "filesystem": filesystem_configured,
                        "filesystem_mount": filesystem_mount,
                        "filesystem_rootfs": filesystem_rootfs,
                        "filesystem_tmpfs": filesystem_tmpfs,
                        "privilege": privilege_configured,
                        "resources": resources_configured,
                        "image": image_configured,
                        "identity": identity_configured,
                        "ownership": ownership_configured,
                    }
                    inspected_contract["mount_count"] = len(mounts) if isinstance(mounts, list) else -1
                    inspected_contract["mount_destinations_safe"] = sorted(
                        str(item.get("Destination", ""))
                        for item in mounts
                        if isinstance(item, Mapping)
                    )
                    inspected_contract["mount_all_readonly"] = bool(
                        isinstance(mounts, list)
                        and all(item.get("RW") is False for item in mounts if isinstance(item, Mapping))
                    )
                    probe_mount = next(
                        (item for item in mounts if isinstance(item, Mapping) and item.get("Destination") == "/probe"),
                        None,
                    )
                    inspected_contract["probe_mount_type_ok"] = bool(
                        isinstance(probe_mount, Mapping) and probe_mount.get("Type") == "bind"
                    )
                    inspected_contract["probe_mount_source_match"] = bool(
                        isinstance(probe_mount, Mapping)
                        and probe_mount.get("RW") is False
                        and _inspect_mount_contract(
                            [probe_mount],
                            {
                                "Binds": None,
                                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=32m,mode=1777"},
                                "SecurityOpt": ["no-new-privileges"],
                            },
                            {"/probe": probe_root},
                        )
                    )
                    if isinstance(probe_mount, Mapping):
                        source_value = str(probe_mount.get("Source", ""))
                        inspected_contract["probe_source_style"] = (
                            "windows" if re.fullmatch(r"[A-Za-z]:[\\/].+", source_value)
                            else "host_mnt" if source_value.startswith("/host_mnt/")
                            else "desktop_host" if source_value.startswith("/run/desktop/mnt/host/")
                            else "other"
                        )
                    if not all(inspected_contract.values()):
                        failure_reason = "sandbox_config_rejected"
            if not failure_reason:
                probe_result = _run_bounded_command(
                    [*docker_command, "start", "--attach", container_id],
                    env=_safe_docker_environment(),
                    timeout=20,
                    limit=8192,
                )
    except Exception:
        external_uncertain = True
        failure_reason = failure_reason or "sandbox_probe_failed"
    except BaseException:
        # Preserve interrupts, but make the cleanup gate observe the command
        # boundary as unknown before the surrounding ``finally`` executes.
        external_uncertain = True
        raise
    finally:
        cleanup_reference = container_id if container_owned else ""
        if not cleanup_reference:
            # A timed-out create can leave a container even when no id was
            # returned.  If an id exists but full inspection failed, re-check
            # only its ownership label; a name/ID collision is never deleted.
            ownership_target = container_id or container_name
            try:
                ownership_check = _run_bounded_command(
                    [
                        *docker_command,
                        "container",
                        "inspect",
                        "--format={{.Id}}|{{.Name}}|{{.Config.Image}}|{{index .Config.Labels \"brain-memory.p7.nonce\"}}",
                        ownership_target,
                    ],
                    env=_safe_docker_environment(),
                    timeout=3,
                    limit=4096,
                )
                if (
                    ownership_check.returncode == 0
                    and not ownership_check.timed_out
                    and not ownership_check.overflow
                    and ownership_check.drained
                ):
                    owner_fields = ownership_check.stdout.decode(
                        "utf-8", errors="strict"
                    ).strip().split("|", 3)
                    owner_id, owner_name, owner_image, owner_nonce = (
                        owner_fields if len(owner_fields) == 4 else ("", "", "", "")
                    )
                    if (
                        _FULL_CONTAINER_ID_RE.fullmatch(owner_id.lower())
                        and owner_name in {container_name, "/" + container_name}
                        and owner_image in {image, image_id}
                        and owner_nonce == probe_nonce
                    ):
                        # Never delete by a reusable name.  The immutable ID
                        # returned by this ownership check is the only cleanup
                        # target when create did not yield an ID.
                        cleanup_reference = owner_id.lower()
                elif (
                    ownership_check.returncode != 0
                    and not ownership_check.timed_out
                    and not ownership_check.overflow
                    and ownership_check.drained
                ):
                    # A generic inspect failure is not proof that the
                    # container is absent.  Leave cleanup unverified and let
                    # the outer failure path remain blocked.
                    cleanup_verified = False
            except Exception:
                external_uncertain = True
                ownership_check = _BoundedCommandResult(
                    None, b"", b"", False, False, False
                )
                cleanup_verified = False
        if (
            _FULL_CONTAINER_ID_RE.fullmatch(cleanup_reference)
            and command_boundary_proven()
            and docker_cli_boundary_proven()
            # Keep the endpoint locality probe as the final external check
            # before rm; a context switch must fail closed rather than
            # redirecting an otherwise valid ID.
            and endpoint_boundary_proven()
        ):
            try:
                cleanup_result = _run_bounded_command(
                    [
                        *docker_command,
                        "container",
                        "rm",
                        "--force",
                        cleanup_reference,
                    ],
                    env=_safe_docker_environment(),
                    timeout=5,
                    limit=4096,
                )
                if _bounded_success(cleanup_result):
                    # ``inspect`` exits non-zero for both "not found" and
                    # daemon/permission failures.  Query the bounded list
                    # endpoint instead and require a successful empty result;
                    # this avoids treating an arbitrary error as proof of
                    # deletion.
                    # Query by the invocation nonce rather than only the
                    # container ID.  A delayed daemon create can produce a
                    # different ID after the first container has been
                    # removed; the nonce is the immutable ownership key for
                    # this probe.
                    selector = "label=brain-memory.p7.nonce=" + probe_nonce
                    post_cleanup = _run_bounded_command(
                        [
                            *docker_command,
                            "container",
                            "ls",
                            "--all",
                            "--no-trunc",
                            "--filter",
                            selector,
                            "--format={{.ID}}|{{.Names}}",
                        ],
                        env=_safe_docker_environment(),
                        timeout=3,
                        limit=4096,
                    )
                    post_cleanup2 = _run_bounded_command(
                        [
                            *docker_command,
                            "container",
                            "ls",
                            "--all",
                            "--no-trunc",
                            "--filter",
                            selector,
                            "--format={{.ID}}|{{.Names}}",
                        ],
                        env=_safe_docker_environment(),
                        timeout=3,
                        limit=4096,
                    )
                    cleanup_verified = (
                        _bounded_success(post_cleanup)
                        and not post_cleanup.stdout.strip()
                        and _bounded_success(post_cleanup2)
                        and not post_cleanup2.stdout.strip()
                        and endpoint_boundary_proven()
                    )
            except Exception:
                external_uncertain = True
                cleanup_result = _BoundedCommandResult(
                    None, b"", b"", False, False, False
                )
                cleanup_verified = False
        # A create request may be accepted by the daemon after its CLI is
        # terminated, so one name lookup is not enough.  Once the bounded
        # create process has a return code and its tree/pipe boundary is
        # proven closed, use the nonce-scoped immutable-ID reaper.  If either
        # condition is uncertain, keep cleanup blocked rather than racing a
        # live process or deleting by name.
        if (
            not cleanup_verified
            and command_boundary_proven()
            and create_result is not None
            and create_result.returncode is not None
            and create_result.drained
            and all(
                result is None
                or (result.returncode is not None and _bounded_finished(result))
                for result in (
                    inspect_result,
                    probe_result,
                    ownership_check,
                    cleanup_result,
                    post_cleanup,
                    post_cleanup2,
                )
            )
        ):
            try:
                cleanup_verified = _reap_owned_eval_container(
                    docker,
                    context,
                    probe_nonce,
                    image,
                    name_prefix="brain-memory-p7-probe-",
                    expected_endpoint_digest=endpoint_digest,
                    expected_docker_cli_digest=docker_cli_digest,
                )
            except Exception:
                cleanup_verified = False
        if lease is not None and lease_store is not None:
            if cleanup_verified:
                try:
                    process_stopped = all(
                        result is None or _bounded_process_stopped(result)
                        for result in (
                            create_result,
                            inspect_result,
                            probe_result,
                            ownership_check,
                            cleanup_result,
                            post_cleanup,
                            post_cleanup2,
                        )
                    ) and not external_uncertain
                    if not process_stopped or not lease_store.mark_cleanup_pending(
                        lease, process_stopped=True
                    ) or not lease_store.complete(lease):
                        cleanup_verified = False
                        failure_reason = failure_reason or "orphan_lease_unavailable"
                except Exception:
                    cleanup_verified = False
                    failure_reason = failure_reason or "orphan_lease_unavailable"
            else:
                marked_stopped = lease_store.mark_cleanup_pending(
                    lease,
                    process_stopped=(
                        all(
                            result is None or _bounded_process_stopped(result)
                            for result in (
                                create_result,
                                inspect_result,
                                probe_result,
                                ownership_check,
                                cleanup_result,
                                post_cleanup,
                                post_cleanup2,
                            )
                        )
                        and not external_uncertain
                    ),
                )
                if not marked_stopped:
                    failure_reason = failure_reason or "orphan_lease_unavailable"
    if failure_reason:
        return blocked(
            failure_reason,
            daemon=True,
            image_pinned=True,
            image_available=True,
            contract_checks=inspected_contract
            if failure_reason == "sandbox_config_rejected"
            else None,
        )
    if not cleanup_verified:
        return blocked(
            "sandbox_cleanup_failed",
            daemon=True,
            image_pinned=True,
            image_available=True,
        )
    if (
        probe_result is None
        or probe_result.timed_out
        or probe_result.overflow
        or not probe_result.drained
        or len(probe_result.stdout) > 8192
    ):
        return blocked(
            "sandbox_probe_invalid",
            daemon=True,
            image_pinned=True,
            image_available=True,
        )
    try:
        probe = json.loads(probe_result.stdout.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeError, ValueError, IndexError):
        probe = None
    required = (
        "network_isolation_verified",
        "filesystem_isolation_verified",
        "privilege_isolation_verified",
        "temporary_write_verified",
    )
    verified = bool(
        probe_result.returncode == 0
        and not probe_result.timed_out
        and not probe_result.overflow
        and probe_result.drained
        and isinstance(probe, Mapping)
        and probe.get("schema_version") == 1
        and probe.get("ready") is True
        and all(probe.get(name) is True for name in required)
    )
    if not verified:
        return blocked(
            "sandbox_probe_rejected",
            daemon=True,
            image_pinned=True,
            image_available=True,
        )
    inspected_digest = _digest(
        _canonical(_normalized_container_contract(inspected_contract))
    )
    endpoint_kind = endpoint.split(":", 1)[0].lower()
    contract_digest = _digest(
        _canonical(
            {
                "protocol": _SANDBOX_PROTOCOL,
                "context": context,
                "endpoint_kind": endpoint_kind,
                "endpoint_digest": endpoint_digest,
                "image": image,
                "image_id": image_id,
                "security_flags": _DOCKER_SECURITY_FLAGS,
                "tmpfs": _DOCKER_TMPFS_FLAG,
                "inspected_contract": inspected_digest,
                "probe_schema": 1,
            }
        )
    )
    return {
        "ready": True,
        "executor": "docker",
        "protocol": _SANDBOX_PROTOCOL,
        "local_context_verified": True,
        "context_digest": _digest(context),
        "endpoint_kind": endpoint_kind,
        "endpoint_digest": endpoint_digest,
        "daemon_reachable": True,
        "image_pinned": True,
        "image_available": True,
        "probe_verified": True,
        "cleanup_verified": True,
        "network_isolation_verified": True,
        "filesystem_isolation_verified": True,
        "privilege_isolation_verified": True,
        "resource_limits_configured": True,
        "image_reference_digest": _digest(image),
        "image_id_digest": _digest(image_id),
        "container_config_digest": inspected_digest,
        "contract_digest": contract_digest,
        "reason": "",
    }


@dataclass(frozen=True, slots=True)
class ModelPatch:
    """A validated model proposal and its optional raw-response digest.

    ``unified_diff`` is the exact patch that the evaluator applies.  When a
    provider emits a structurally valid but line-ending/Unicode-damaged diff,
    the host may canonicalize it *only* from the model's own replacement
    expression; ``source_diff_hash`` keeps the original response bound in the
    receipt without persisting its raw contents.
    """

    title: str
    scope: str
    hypothesis: str
    unified_diff: str
    source_diff_hash: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "scope": self.scope,
            "hypothesis": self.hypothesis,
            "unified_diff": self.unified_diff,
        }

    @property
    def diff_hash(self) -> str:
        return _digest(self.unified_diff)

    @property
    def model_response_hash(self) -> str:
        """Digest of the provider diff before any structural canonicalization."""

        return self.source_diff_hash or self.diff_hash


def _diff_paths(unified_diff: str) -> tuple[list[str], list[str]]:
    old: list[str] = []
    new: list[str] = []
    for line in unified_diff.splitlines():
        if line.startswith("--- "):
            old.append(line[4:].split("\t", 1)[0].strip())
        elif line.startswith("+++ "):
            new.append(line[4:].split("\t", 1)[0].strip())
    return old, new


def validate_model_patch(value: Mapping[str, Any], *, scope: str = TARGET_SCOPE) -> ModelPatch:
    """Validate a one-file, low-risk unified diff from the external model."""

    if not isinstance(value, Mapping):
        raise ValueError("model response must be an object")
    title = _safe_text(value.get("title"), 180)
    declared_scope = _safe_text(value.get("scope"), 160).replace("\\", "/")
    hypothesis = _safe_text(value.get("hypothesis"), 1_000)
    raw_diff = value.get("unified_diff")
    diff = str(raw_diff or "").replace("\x00", "")[:64_000]
    # Git's patch reader requires a complete final hunk line.  Normalize only
    # the outer whitespace; do not let the generic text helper remove the
    # terminating newline from an otherwise valid model diff.
    diff = diff.strip() + ("\n" if diff.strip() else "")
    expected_scope = scope.replace("\\", "/").strip()
    if not title or not hypothesis or not diff:
        raise ValueError("model patch requires title, hypothesis, and unified_diff")
    if declared_scope != expected_scope:
        raise ValueError("model patch scope is outside the approved low-risk file")
    if (
        not expected_scope
        or expected_scope.startswith(("/", "\\"))
        or ":" in expected_scope
        or ".." in Path(expected_scope).parts
    ):
        raise ValueError("invalid patch scope")
    scope_tokens = set(expected_scope.lower().split("/"))
    if scope_tokens & _PROTECTED_SCOPE_WORDS:
        raise ValueError("patch scope is constitutionally protected")
    if not re.search(r"^--- a/" + re.escape(expected_scope) + r"(?:\t.*)?$", diff, re.MULTILINE):
        raise ValueError("unified diff has no approved source path")
    if not re.search(r"^\+\+\+ b/" + re.escape(expected_scope) + r"(?:\t.*)?$", diff, re.MULTILINE):
        raise ValueError("unified diff has no approved destination path")
    old_paths, new_paths = _diff_paths(diff)
    if old_paths != [f"a/{expected_scope}"] or new_paths != [f"b/{expected_scope}"]:
        raise ValueError("unified diff must contain exactly one approved file")
    if "@@" not in diff or "Binary files" in diff:
        raise ValueError("unified diff is incomplete")
    # A bare ``@@`` is accepted by neither Git nor this host's apply gate.
    # Do not infer line numbers from model output; ask for a fresh patch.
    if not re.search(
        r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?$",
        diff,
        re.MULTILINE,
    ):
        raise ValueError("unified diff must use numbered git hunk headers")
    unsafe_text = "\n".join((title, hypothesis, diff))
    if (
        _URI_RE.search(unsafe_text)
        or _ABSOLUTE_PATH_RE.search(unsafe_text)
        or _SECRET_VALUE_RE.search(unsafe_text)
    ):
        raise ValueError("unified diff contains a path or secret-shaped value")
    lowered = diff.lower()
    for forbidden in (
        "subprocess",
        "os.system",
        "eval(",
        "exec(",
        "urllib",
        "requests.",
        "socket",
        "open(",
        "write_text(",
        "write_bytes(",
        "git push",
        "http://",
        "https://",
    ):
        if forbidden in lowered:
            raise ValueError("low-risk patch contains a forbidden capability")
    changed = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if not changed or len(changed) > 40:
        raise ValueError("patch change size is outside the bounded contract")
    removed = [line[1:] for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    if not any(
        "entities_pool" in line
        and "hash(str(current_tick) + drive_name)" in line
        for line in removed
    ):
        raise ValueError("patch does not replace the observed randomized entity lookup")
    # The first candidate is specifically a deterministic replay repair.  A
    # model may choose the exact SHA-256 expression, but it must make the
    # intent visible and cannot leave the process-randomized call in place.
    if "hashlib" not in lowered or "hash(" not in lowered:
        raise ValueError("patch does not describe the deterministic hash repair")
    return ModelPatch(
        title,
        expected_scope,
        hypothesis,
        diff,
        source_diff_hash=_digest(diff),
    )


def coerce_model_patch(value: Any) -> ModelPatch | None:
    """Return a validated patch or ``None``; never manufacture a fallback."""

    try:
        return validate_model_patch(value)
    except (TypeError, ValueError):
        return None


def public_report(value: Any) -> Any:
    """Return the same bounded public projection used by the API boundary."""

    from brain.public_projection import sanitize_public_projection

    result = sanitize_public_projection(value)
    # The generic projection redacts file-shaped strings.  This one field is
    # different: it is reconstructed from the immutable host allowlist, never
    # copied from an arbitrary caller path, so the public receipt can identify
    # the approved target without exposing a capability-bearing path.
    if isinstance(result, dict) and isinstance(value, Mapping):
        raw_target = value.get("target")
        if isinstance(raw_target, Mapping):
            try:
                target = resolve_iteration_target(raw_target.get("target_id"))
            except (TypeError, ValueError):
                target = None
            if (
                target is not None
                and raw_target.get("scope") == target.scope
                and raw_target.get("contract_digest") == target_contract_digest(target)
            ):
                result["target"] = target.public_summary()
    return result if isinstance(result, dict) else {"value": result}


def replay_probe(root: Path | str, *, seeds: Sequence[int] = DEFAULT_SEEDS) -> dict[str, Any]:
    """Run the same GoalGenerator input in independent hash-seeded interpreters."""

    root_path = Path(root).expanduser().resolve(strict=True)
    if not root_path.is_dir() or not (root_path / TARGET_SCOPE).is_file():
        raise ValueError("probe root must contain the approved target")
    seed_values = tuple(int(seed) for seed in seeds)
    if not seed_values or len(seed_values) > 16:
        raise ValueError("probe seed set is outside the bounded contract")
    code = (
        "import json\n"
        "from brain.drive_engine import GoalGenerator\n"
        "activation = {'survival_drive': 0.0, 'curiosity_drive': 1.0, "
        "'coherence_drive': 0.0, 'growth_drive': 0.0, 'exploration_drive': 0.0, "
        "'creation_drive': 0.0, 'connection_drive': 0.0}\n"
        "goals = GoalGenerator().generate(activation, ['identity'], ['Alpha', 'Beta'], [], 0, 17)\n"
        "print(json.dumps([goal.description for goal in goals], ensure_ascii=False))\n"
    )
    descriptions: list[tuple[str, ...]] = []
    observations: list[dict[str, Any]] = []
    for seed in seed_values:
        completed = _run_bounded_command(
            [sys.executable, "-c", code],
            cwd=root_path,
            env=_safe_child_environment(root=root_path, hash_seed=seed),
            timeout=8,
            limit=65536,
            capture_stderr=True,
        )
        if (
            completed.returncode != 0
            or completed.timed_out
            or completed.overflow
            or not completed.drained
        ):
            raise RuntimeError(f"replay probe child failed for seed {seed}")
        parsed = json.loads(completed.stdout.decode("utf-8", errors="strict").strip().splitlines()[-1])
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise RuntimeError("replay probe returned an invalid description list")
        values = tuple(parsed)
        descriptions.append(values)
        observations.append(
            {
                "seed": seed,
                "description_digest": _digest(_canonical(values)),
                "description_count": len(values),
            }
        )
    unique = {values for values in descriptions}
    return {
        "source_kind": "runtime_replay",
        "seed_count": len(seed_values),
        "observations": observations,
        "unique_description_count": len(unique),
        "observation_digest": _digest(_canonical(observations)),
        "replayable": len(unique) == 1,
    }


def _executor_config_from_fixtures(fixtures: Path | str) -> tuple[str, str, str]:
    fixture_root = Path(fixtures).expanduser().resolve(strict=True)
    config_path = fixture_root / "executor.json"
    if _is_link_like(config_path) or not config_path.is_file():
        raise ValueError("sandbox executor config is unavailable")
    if config_path.stat(follow_symlinks=False).st_size > 4096:
        raise ValueError("sandbox executor config exceeds its bounded size")
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("sandbox executor config is invalid") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != HOST_SCHEMA_VERSION
        or value.get("protocol") != _SANDBOX_PROTOCOL
    ):
        raise ValueError("sandbox executor config is invalid")
    image = _configured_docker_image(value.get("image"))
    context = _safe_text(value.get("context"), 81)
    wrapper_hash = _safe_text(value.get("wrapper_sha256"), 128).lower()
    judge_hash = _safe_text(value.get("judge_sha256"), 128).lower()
    docker = _configured_docker_cli(_safe_text(value.get("docker_cli"), 1000))
    docker_hash = _safe_text(value.get("docker_cli_sha256"), 128).lower()
    if (
        not _DOCKER_IMAGE_RE.fullmatch(image)
        or not _DOCKER_CONTEXT_RE.fullmatch(context)
        or not re.fullmatch(r"[0-9a-f]{64}", wrapper_hash)
        or not re.fullmatch(r"[0-9a-f]{64}", judge_hash)
        or not docker
        or not re.fullmatch(r"[0-9a-f]{64}", docker_hash)
    ):
        raise ValueError("sandbox executor config is invalid")
    wrapper = fixture_root / "docker_executor.py"
    judge = fixture_root / "judge.py"
    if (
        _bounded_file_digest(wrapper) != wrapper_hash
        or _bounded_file_digest(judge) != judge_hash
        or _bounded_file_digest(docker, max_bytes=_DOCKER_CLI_MAX_BYTES)
        != docker_hash
    ):
        raise ValueError("sandbox executor fixture code changed")
    return image, context, docker


def _executor_image_from_fixtures(fixtures: Path | str) -> str:
    """Compatibility helper for tests and read-only diagnostics."""

    return _executor_config_from_fixtures(fixtures)[0]


def _reap_owned_eval_container(
    docker: str,
    context: str,
    nonce: str,
    image: str,
    *,
    name_prefix: str = "brain-memory-p7-eval-",
    expected_endpoint_digest: str | None = None,
    expected_docker_cli_digest: str | None = None,
    allow_removal: bool = True,
) -> bool:
    """Remove one orphaned evaluation container, only after proving ownership.

    The wrapper normally removes its container in ``finally``.  The outer
    bounded runner can nevertheless terminate the wrapper before that block
    runs (for example at the timeout boundary).  This recovery path is kept
    deliberately narrow: it queries a nonce label, requires exactly one
    container with the protocol-generated name (``eval`` by default; the
    capability probe uses its fixed ``probe`` prefix), verifies the inspected
    label and image, removes that exact immutable ID, and proves an empty
    post-query.
    Any uncertain Docker result is a failed cleanup rather than permission to
    broaden the deletion target.
    """

    if (
        not isinstance(docker, str)
        or not Path(docker).is_absolute()
        or not _DOCKER_CONTEXT_RE.fullmatch(str(context or ""))
        or not re.fullmatch(r"[0-9a-f]{32,128}", str(nonce or ""))
        or not _DOCKER_IMAGE_RE.fullmatch(str(image or ""))
        or not re.fullmatch(r"brain-memory-p7-(?:eval|probe)-", str(name_prefix or ""))
        or not isinstance(allow_removal, bool)
        or (
            expected_endpoint_digest is not None
            and not re.fullmatch(r"[0-9a-f]{64}", str(expected_endpoint_digest or ""))
        )
        or (
            expected_docker_cli_digest is not None
            and not re.fullmatch(
                r"[0-9a-f]{64}", str(expected_docker_cli_digest or "")
            )
        )
    ):
        return False
    try:
        pinned_docker_cli_digest = _bounded_file_digest(
            docker, max_bytes=_DOCKER_CLI_MAX_BYTES
        )
    except (OSError, ValueError):
        return False
    if (
        expected_docker_cli_digest is not None
        and pinned_docker_cli_digest != str(expected_docker_cli_digest).lower()
    ):
        return False

    def docker_cli_matches() -> bool:
        try:
            return (
                _bounded_file_digest(docker, max_bytes=_DOCKER_CLI_MAX_BYTES)
                == pinned_docker_cli_digest
            )
        except (OSError, ValueError):
            return False

    # The fixture validates locality before creating the container, but the
    # context could be changed while a timed-out wrapper is being reaped.
    # Re-check the endpoint and refuse to issue any remote cleanup command.
    initial_endpoint = _local_docker_context_endpoint(docker, context)
    if not initial_endpoint:
        return False
    pinned_endpoint_digest = _digest(initial_endpoint)
    if (
        expected_endpoint_digest is not None
        and pinned_endpoint_digest != str(expected_endpoint_digest).lower()
    ):
        return False
    expected_name = name_prefix + nonce[:32]
    command = [docker, "--context", context, "container"]

    def endpoint_matches() -> bool:
        if not docker_cli_matches():
            return False
        current = _local_docker_context_endpoint(docker, context)
        return bool(current and _digest(current) == pinned_endpoint_digest)

    def owned_listing() -> list[tuple[str, str]] | None:
        try:
            if not endpoint_matches():
                return None
            result = _run_bounded_command(
                [
                    *command,
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "label=brain-memory.p7.nonce=" + nonce,
                    "--format={{.ID}}|{{.Names}}",
                ],
                env=_safe_docker_environment(),
                timeout=3,
                limit=4096,
            )
            if not _bounded_success(result):
                return None
            text = result.stdout.decode("utf-8", errors="strict")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) > 1:
                return None
            if not lines:
                return []
            parts = lines[0].split("|", 1)
            if len(parts) != 2:
                return None
            container_id, rendered_name = (part.strip() for part in parts)
            if not _FULL_CONTAINER_ID_RE.fullmatch(container_id):
                return None
            if rendered_name not in {expected_name, "/" + expected_name}:
                return None
            return [(container_id, rendered_name)]
        except (OSError, UnicodeError, ValueError, TypeError):
            return None

    listed = owned_listing()
    if listed is None:
        return False
    # A successful, nonce-scoped empty query is proof that no owned container
    # remains only after a second consecutive empty query.  The second query
    # closes the small daemon race in which a create request can be accepted
    # just after the wrapper process is terminated.
    if not listed:
        return owned_listing() == []
    if allow_removal is not True:
        return False
    container_id, rendered_name = listed[0]
    try:
        if not endpoint_matches():
            return False
        inspected_result = _run_bounded_command(
            [
                *command,
                "inspect",
                "--format={{json .}}",
                container_id,
            ],
            env=_safe_docker_environment(),
            timeout=5,
            limit=262144,
        )
        if not _bounded_success(inspected_result):
            return False
        inspected = json.loads(
            inspected_result.stdout.decode("utf-8", errors="strict")
        )
        if not isinstance(inspected, Mapping):
            return False
        config = inspected.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        inspected_name = inspected.get("Name")
        configured_image = config.get("Image") if isinstance(config, Mapping) else None
        inspected_id = str(inspected.get("Id", "")).lower()
        if (
            not _FULL_CONTAINER_ID_RE.fullmatch(inspected_id)
            or not _FULL_CONTAINER_ID_RE.fullmatch(container_id)
            or inspected_id != container_id.lower()
            or inspected_name not in {expected_name, "/" + expected_name}
            or rendered_name not in {expected_name, "/" + expected_name}
            or not isinstance(labels, Mapping)
            or labels.get("brain-memory.p7.nonce") != nonce
            or configured_image != image
        ):
            return False
        # Re-check locality immediately before the destructive operation.  A
        # context update between the initial probe and this point must fail
        # closed instead of redirecting cleanup to another daemon.
        # The endpoint locality check is the final external observation before
        # the destructive command; the CLI digest is checked first and again
        # inside endpoint_matches.
        if not docker_cli_matches() or not endpoint_matches():
            return False
        removed = _run_bounded_command(
            [*command, "rm", "--force", container_id],
            env=_safe_docker_environment(),
            timeout=5,
            limit=4096,
        )
        if not _bounded_success(removed):
            return False
        remaining = owned_listing()
        return remaining == [] and owned_listing() == []
    except Exception:
        return False


def _sandbox_replay_probe(
    root: Path | str,
    fixtures: Path | str,
    *,
    sandbox: Mapping[str, Any] | None = None,
    lease_store: OrphanLeaseStore | None = None,
    lease_run_id: str = "",
) -> dict[str, Any]:
    """Collect replay evidence through the same fixture-owned Docker runner."""

    root_path = Path(root).expanduser().resolve(strict=True)
    fixture_root = Path(fixtures).expanduser().resolve(strict=True)
    wrapper = fixture_root / "docker_executor.py"
    if (
        not root_path.is_dir()
        or not (root_path / TARGET_SCOPE).is_file()
        or _is_link_like(wrapper)
        or not wrapper.is_file()
    ):
        raise ValueError("sandbox replay inputs are invalid")
    # Generated fixtures carry hashes for both executable pieces.  Keep a
    # compatibility path for narrow unit-test doubles that intentionally omit
    # executor.json; real P7 runs always take the authenticated branch.
    request: dict[str, Any] = {}
    executor_image = ""
    executor_context = ""
    executor_docker = ""
    executor_docker_digest = ""
    executor_endpoint_digest = ""
    config_path = fixture_root / "executor.json"
    if config_path.is_file():
        executor_image, executor_context, executor_docker = _executor_config_from_fixtures(
            fixture_root
        )
        wrapper_hash = _bounded_file_digest(wrapper)
        judge_hash = _bounded_file_digest(fixture_root / "judge.py")
        config_hash = _bounded_file_digest(config_path)
        try:
            persisted_config = json.loads(
                config_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError):
            raise RuntimeError("sandbox replay executor config is invalid") from None
        persisted_docker_digest = (
            _safe_text(persisted_config.get("docker_cli_sha256"), 128).lower()
            if isinstance(persisted_config, Mapping)
            else ""
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", persisted_docker_digest)
            or _bounded_file_digest(config_path) != config_hash
            or _bounded_file_digest(
                executor_docker, max_bytes=_DOCKER_CLI_MAX_BYTES
            )
            != persisted_docker_digest
        ):
            raise RuntimeError("sandbox replay executor binding changed")
        executor_docker_digest = persisted_docker_digest
        request = {
            "schema_version": HOST_SCHEMA_VERSION,
            "protocol": _SANDBOX_PROTOCOL,
            "nonce": secrets.token_hex(16),
            "candidate_target_sha256": _bounded_file_digest(
                root_path / TARGET_SCOPE
            ),
            "wrapper_sha256": wrapper_hash,
            "judge_sha256": judge_hash,
            "executor_config_sha256": config_hash,
        }
        executor_endpoint = _local_docker_context_endpoint(
            executor_docker, executor_context
        )
        if not executor_endpoint:
            raise RuntimeError("sandbox replay endpoint is unavailable")
        executor_endpoint_digest = _digest(executor_endpoint)
        request["endpoint_digest"] = executor_endpoint_digest
        if sandbox is not None:
            for request_key, sandbox_key in (
                ("sandbox_contract_digest", "contract_digest"),
                ("container_config_digest", "container_config_digest"),
                ("image_id_digest", "image_id_digest"),
                ("endpoint_digest", "endpoint_digest"),
            ):
                digest = _safe_text(sandbox.get(sandbox_key), 128).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("sandbox replay binding is incomplete")
                request[request_key] = digest
            if request["endpoint_digest"] != executor_endpoint_digest:
                raise RuntimeError("sandbox replay endpoint binding changed")
    lease: OrphanLeaseHandle | None = None
    if lease_store is not None:
        # A real replay always has the authenticated executor envelope.  The
        # compatibility path is intentionally not allowed to launch an
        # untracked Docker operation when durable lease tracking is requested.
        if not request:
            raise RuntimeError("sandbox replay lease unavailable")
        try:
            lease = lease_store.arm(
                operation="eval",
                nonce=request["nonce"],
                name="brain-memory-p7-eval-" + request["nonce"][:32],
                image=executor_image,
                context=executor_context,
                docker_cli=executor_docker,
                endpoint_digest=executor_endpoint_digest,
                run_id=lease_run_id,
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError("sandbox replay lease unavailable") from exc
    child_env = _safe_docker_environment()
    if request:
        child_env["P7_EXECUTION_REQUEST"] = _canonical(request)

    def replay_failure(message: str, cause: BaseException | None = None) -> None:
        """Fail closed after attempting nonce-scoped orphan cleanup."""

        # A bounded result with ``drained=False`` or no return code does not
        # prove that the wrapper process tree has stopped.  Do not race a
        # still-running wrapper by treating an empty label query as cleanup.
        process_stopped = (
            completed is not None
            and completed.returncode is not None
            and completed.drained
        )
        if request and process_stopped:
            try:
                cleaned = _reap_owned_eval_container(
                    executor_docker,
                    executor_context,
                    request["nonce"],
                    executor_image,
                    expected_endpoint_digest=executor_endpoint_digest,
                    expected_docker_cli_digest=executor_docker_digest,
                )
            except Exception:
                cleaned = False
            if not cleaned:
                error = RuntimeError("sandbox replay cleanup unverified")
                if cause is not None:
                    raise error from cause
                raise error
            if lease is not None and lease_store is not None:
                if (
                    not lease_store.mark_cleanup_pending(lease, process_stopped=True)
                    or not lease_store.complete(lease)
                ):
                    error = RuntimeError("sandbox replay cleanup lease unverified")
                    if cause is not None:
                        raise error from cause
                    raise error
        elif lease is not None and lease_store is not None:
            lease_store.mark_cleanup_pending(
                lease,
                process_stopped=bool(
                    completed is not None
                    and (
                        completed.returncode is not None
                        and completed.drained
                    )
                ),
            )
        error = RuntimeError(message)
        if cause is not None:
            raise error from cause
        raise error

    completed: _BoundedCommandResult | None = None
    try:
        completed = _run_bounded_command(
            [sys.executable, "-I", str(wrapper), str(root_path), str(fixture_root)],
            cwd=fixture_root,
            env=child_env,
            timeout=60,
            limit=65536,
            capture_stderr=True,
        )
    except Exception as exc:
        replay_failure(
            "sandbox replay executor failed: " + _diagnostic_summary(str(exc)), exc
        )
    if (
        completed.returncode != 0
        or completed.timed_out
        or completed.overflow
        or not completed.drained
        or not completed.stdout
    ):
        replay_failure(
            "sandbox replay executor failed: "
            + _diagnostic_summary(
                    "returncode=%s timed_out=%s overflow=%s drained=%s stderr=%s stdout_tail=%s"
                % (
                    completed.returncode,
                    completed.timed_out,
                    completed.overflow,
                    completed.drained,
                    completed.stderr.decode("utf-8", errors="replace"),
                    completed.stdout.decode("utf-8", errors="replace")[-1024:],
                )
            )
        )
    try:
        payload = json.loads(
            completed.stdout.decode("utf-8", errors="strict").strip().splitlines()[-1]
        )
    except (UnicodeError, ValueError, IndexError) as exc:
        replay_failure("sandbox replay result is invalid", exc)
    if not isinstance(payload, Mapping) or payload.get("verified") is not True:
        replay_failure("sandbox replay result is unverified")
    execution_metadata = payload.get("metadata", {})
    if request:
        if not isinstance(execution_metadata, Mapping):
            replay_failure("sandbox replay execution proof is missing")
        if (
            _safe_text(execution_metadata.get("nonce"), 128) != request["nonce"]
            or _safe_text(execution_metadata.get("request_digest"), 128).lower()
            != _digest(_canonical(request))
            or _safe_text(execution_metadata.get("wrapper_sha256"), 128).lower()
            != request["wrapper_sha256"]
            or _safe_text(execution_metadata.get("judge_sha256"), 128).lower()
            != request["judge_sha256"]
            or _safe_text(execution_metadata.get("executor_config_sha256"), 128).lower()
            != request["executor_config_sha256"]
            or _safe_text(
                execution_metadata.get("candidate_target_sha256_before"), 128
            ).lower()
            != request["candidate_target_sha256"]
            or _safe_text(
                execution_metadata.get("candidate_target_sha256_after"), 128
            ).lower()
            != request["candidate_target_sha256"]
            or _safe_text(execution_metadata.get("judge_sha256_before"), 128).lower()
            != request["judge_sha256"]
            or _safe_text(execution_metadata.get("judge_sha256_after"), 128).lower()
            != request["judge_sha256"]
            or execution_metadata.get("input_integrity_verified") is not True
            or execution_metadata.get("inspect_verified") is not True
            or execution_metadata.get("cleanup_verified") is not True
        ):
            replay_failure("sandbox replay execution proof is invalid")
        for key in (
            "sandbox_contract_digest",
            "container_config_digest",
            "image_id_digest",
            "endpoint_digest",
        ):
            if key in request and _safe_text(
                execution_metadata.get(key), 128
            ).lower() != request[key]:
                replay_failure("sandbox replay execution binding changed")
    if lease is not None and lease_store is not None:
        if (
            completed.returncode is None
            or not completed.drained
            or not lease_store.mark_cleanup_pending(lease, process_stopped=True)
            or not lease_store.complete(lease)
        ):
            replay_failure("sandbox replay cleanup lease unverified")
    metrics = payload.get("metrics")
    observations = payload.get("observations")
    if not isinstance(metrics, Mapping) or not isinstance(observations, list):
        replay_failure("sandbox replay result is incomplete")
    if len(observations) != len(DEFAULT_SEEDS):
        replay_failure("sandbox replay seed evidence is incomplete")
    normalized: list[dict[str, Any]] = []
    for expected_seed, raw in zip(DEFAULT_SEEDS, observations):
        if not isinstance(raw, Mapping) or raw.get("seed") != expected_seed:
            replay_failure("sandbox replay seed evidence is invalid")
        digest = _safe_text(raw.get("description_digest"), 128).lower()
        count = raw.get("description_count")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(count, int) or count < 0:
            replay_failure("sandbox replay observation is invalid")
        normalized.append(
            {
                "seed": expected_seed,
                "description_digest": digest,
                "description_count": count,
            }
        )
    try:
        unique = int(metrics.get("unique_description_count"))
        deterministic = float(metrics.get("deterministic_replay"))
    except (TypeError, ValueError, OverflowError) as exc:
        replay_failure("sandbox replay metrics are invalid", exc)
    if unique < 1 or unique > len(DEFAULT_SEEDS) or deterministic not in (0.0, 1.0):
        replay_failure("sandbox replay metrics are outside the contract")
    replayable = unique == 1
    if replayable != (deterministic == 1.0):
        replay_failure("sandbox replay metrics disagree")
    description_counts = [item["description_count"] for item in normalized]
    goal_generation_integrity = bool(
        description_counts
        and all(count > 0 for count in description_counts)
        and len(set(description_counts)) == 1
    )
    result = {
        "source_kind": "runtime_replay",
        "executor": "docker",
        "seed_count": len(DEFAULT_SEEDS),
        "observations": normalized,
        "unique_description_count": unique,
        "goal_generation_integrity": goal_generation_integrity,
        "observation_digest": _digest(_canonical(normalized)),
        "replayable": replayable,
        "execution_metadata": public_report(dict(execution_metadata))
        if isinstance(execution_metadata, Mapping)
        else {},
    }
    return result


def static_gap_probe(root: Path | str) -> dict[str, Any]:
    """Independently inspect the active source for the process-randomized call."""

    root_path = Path(root).expanduser().resolve(strict=True)
    target = root_path / TARGET_SCOPE
    source = target.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=TARGET_SCOPE)
    except SyntaxError as exc:
        raise RuntimeError("target source is not parseable") from exc
    builtin_hash_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hash":
            builtin_hash_lines.append(int(getattr(node, "lineno", 0)))
    stem_source = ""
    stem_path = root_path / "brain" / "brain_stem.py"
    if stem_path.is_file():
        stem_source = stem_path.read_text(encoding="utf-8")
    call_site = bool(re.search(r"(?:self\.)?goal_generator\.generate\s*\(", stem_source))
    return {
        "source_kind": "static_ast",
        "target_digest": _digest(source),
        "call_site_digest": _digest(stem_source),
        "builtin_hash_detected": bool(builtin_hash_lines),
        "builtin_hash_line_count": len(builtin_hash_lines),
        "goal_generator_call_detected": call_site,
        "evidence_digest": _digest(
            _canonical(
                {
                    "builtin_hash": bool(builtin_hash_lines),
                    "call_site": call_site,
                    "target_digest": _digest(source),
                    "call_site_digest": _digest(stem_source),
                }
            )
        ),
    }


def _safe_copy_tree(source: Path | str, destination: Path | str) -> None:
    """Copy a source checkout into a clean runtime tree without private state."""

    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination).expanduser().resolve(strict=False)
    if not source_path.is_dir() or destination_path.exists():
        raise ValueError("copy roots must be a real source and a new destination")
    if source_path == destination_path or source_path in destination_path.parents:
        raise ValueError("destination cannot be inside source")
    destination_path.mkdir(parents=True, exist_ok=False)
    copied_files = 0
    copied_bytes = 0
    for current, dirs, files in os.walk(source_path, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(source_path)
        if len(relative_dir.parts) > _COPY_MAX_DEPTH:
            raise ValueError("source tree exceeds the bounded depth")
        retained_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            if name.lower() in _SKIP_DIRECTORIES:
                continue
            if _is_link_like(path):
                raise ValueError("source contains a link-like directory")
            retained_dirs.append(name)
        dirs[:] = retained_dirs
        (destination_path / relative_dir).mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            source_file = current_path / name
            lower = name.lower()
            if lower.endswith((".pyc", ".pyo")) or lower.endswith(_MUTABLE_SUFFIXES):
                continue
            # Do not even open credential-shaped files.  Reject them by name
            # before stat/copy so their contents never enter a runtime tree.
            if (
                lower in {".env", ".env.local", ".env.production"}
                or _PRIVATE_FILE_RE.search(name)
            ):
                continue
            if _is_link_like(source_file):
                raise ValueError("source contains a link-like file")
            file_stat = source_file.stat(follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode) or getattr(file_stat, "st_nlink", 1) > 1:
                raise ValueError("source contains a non-regular or aliased file")
            copied_files += 1
            copied_bytes += max(0, int(file_stat.st_size))
            if copied_files > _COPY_MAX_FILES or copied_bytes > _COPY_MAX_BYTES:
                raise ValueError("source tree exceeds the bounded copy budget")
            relative_file = source_file.relative_to(source_path)
            target_file = destination_path / relative_file
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file, follow_symlinks=False)
    # The controller performs its own bounded scan; this early check gives a
    # clearer host failure before any lifecycle capability is constructed.
    for current, dirs, files in os.walk(destination_path, followlinks=False):
        for name in (*dirs, *files):
            if _is_link_like(Path(current) / name):
                raise ValueError("copied runtime tree contains a link-like entry")


def _tree_manifest(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = Path(current) / name
            if _is_link_like(path):
                raise ValueError("runtime tree contains a link-like entry")
            relative = path.relative_to(root).as_posix()
            rows[relative] = _digest(path.read_bytes())
    return rows


def _entity_assignment(tree: ast.AST) -> ast.Assign | None:
    matches: list[ast.Assign] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "entity" for target in node.targets):
            matches.append(node)
    return matches[0] if len(matches) == 1 else None


def _entity_lookup(value: ast.AST) -> ast.Subscript | None:
    """Return the pool lookup while preserving the surrounding empty case."""

    if isinstance(value, ast.IfExp):
        value = value.body
    return value if isinstance(value, ast.Subscript) else None


def _approved_index_left() -> ast.AST:
    """Build the one deterministic index expression allowed by P7."""

    return ast.parse(
        "int(hashlib.sha256((str(current_tick) + drive_name).encode(\"utf-8\")).hexdigest(), 16)",
        mode="eval",
    ).body


def _assert_low_risk_transform(baseline: Path, candidate: Path) -> None:
    """Allow only the import plus the deterministic entity-index expression."""

    before_source = (baseline / TARGET_SCOPE).read_text(encoding="utf-8")
    after_source = (candidate / TARGET_SCOPE).read_text(encoding="utf-8")
    before_tree = ast.parse(before_source, filename=TARGET_SCOPE)
    after_tree = ast.parse(after_source, filename=TARGET_SCOPE)
    before_entity = _entity_assignment(before_tree)
    after_entity = _entity_assignment(after_tree)
    if before_entity is None or after_entity is None:
        raise RuntimeError("candidate changed the entity assignment shape")

    class _Normalise(ast.NodeTransformer):
        def visit_Import(self, node: ast.Import):  # type: ignore[override]
            node = self.generic_visit(node)
            node.names = [alias for alias in node.names if alias.name != "hashlib"]
            return node if node.names else None

        def visit_ImportFrom(self, node: ast.ImportFrom):  # type: ignore[override]
            if node.module == "hashlib":
                return None
            return self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign):  # type: ignore[override]
            node = self.generic_visit(node)
            if any(isinstance(target, ast.Name) and target.id == "entity" for target in node.targets):
                lookup = _entity_lookup(node.value)
                if lookup is None or not isinstance(lookup.slice, ast.BinOp):
                    return node
                lookup.slice.left = ast.Name(
                    id="__p7_entity_index__", ctx=ast.Load()
                )
            return node

    normal_before = _Normalise().visit(copy.deepcopy(before_tree))
    normal_after = _Normalise().visit(copy.deepcopy(after_tree))
    if ast.dump(normal_before, include_attributes=False) != ast.dump(
        normal_after, include_attributes=False
    ):
        raise RuntimeError("candidate contains changes outside the deterministic index")

    hashlib_imports = [
        node
        for node in ast.walk(after_tree)
        if isinstance(node, ast.Import)
        and any(alias.name == "hashlib" and alias.asname is None for alias in node.names)
    ]
    if len(hashlib_imports) != 1 or any(
        isinstance(node, ast.ImportFrom) and node.module == "hashlib"
        for node in ast.walk(after_tree)
    ):
        raise RuntimeError("candidate hashlib import is not the approved form")
    value = _entity_lookup(after_entity.value)
    if value is None or not isinstance(value.value, ast.Name) or value.value.id != "entities_pool":
        raise RuntimeError("candidate does not preserve the entities pool lookup")
    index = value.slice
    if not isinstance(index, ast.BinOp) or not isinstance(index.op, ast.Mod):
        raise RuntimeError("candidate index is not bounded by the entities pool")
    if not (
        isinstance(index.right, ast.Call)
        and isinstance(index.right.func, ast.Name)
        and index.right.func.id == "len"
        and len(index.right.args) == 1
        and isinstance(index.right.args[0], ast.Name)
        and index.right.args[0].id == "entities_pool"
    ):
        raise RuntimeError("candidate index has an unexpected bound")
    expected_index_left = _approved_index_left()
    if ast.dump(index.left, include_attributes=False) != ast.dump(
        expected_index_left, include_attributes=False
    ):
        raise RuntimeError("candidate index does not match the approved deterministic expression")


def _target_line_parts(line: str) -> tuple[str, str, str] | None:
    """Split the one-line ``entity`` lookup without interpreting prose.

    The model is allowed to choose the expression, but the surrounding
    assignment is owned by the baseline.  Keeping this parser deliberately
    narrow prevents a malformed diff from smuggling an additional statement
    into the canonical patch.
    """

    text = line.rstrip("\r\n")
    match = re.fullmatch(
        r"(?P<prefix>\s*entity\s*=\s*entities_pool\[)(?P<index>.+?)(?P<suffix>\]\s*if\s+entities_pool\s+else\s+.*)",
        text,
    )
    if match is None:
        return None
    return match.group("prefix"), match.group("index"), match.group("suffix")


def _canonical_model_patch(patch: ModelPatch, baseline: Path) -> ModelPatch:
    """Canonicalize a model diff using only its exact replacement expression.

    LLMs occasionally return a diff whose hunk context or non-ASCII fallback
    text is damaged even though the intended replacement is clear.  We do not
    invent a replacement or silently broaden the scope: the raw response must
    contain exactly one ``import hashlib`` addition and one ``entity`` line,
    and the latter's index must parse to the approved AST.  The host then
    regenerates a deterministic diff against the real baseline, preserving
    the baseline's untouched ``else`` branch and line endings.
    """

    target = baseline / TARGET_SCOPE
    if not target.is_file() or _is_link_like(target):
        raise RuntimeError("model baseline target is unavailable")
    before_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    target_indices = [
        index
        for index, line in enumerate(before_lines)
        if "hash(str(current_tick) + drive_name)" in line
    ]
    if len(target_indices) != 1:
        raise RuntimeError("model baseline has an ambiguous randomized lookup")
    import_indices = [
        index for index, line in enumerate(before_lines) if line.strip() == "import hashlib"
    ]
    if import_indices:
        raise RuntimeError("model baseline already imports hashlib")
    baseline_parts = _target_line_parts(before_lines[target_indices[0]])
    if baseline_parts is None:
        raise RuntimeError("model baseline lookup shape is unsupported")
    _prefix, baseline_index_text, _suffix = baseline_parts
    baseline_index = ast.parse(baseline_index_text, mode="eval").body
    expected_old_index = ast.parse(
        "hash(str(current_tick) + drive_name) % len(entities_pool)", mode="eval"
    ).body
    if ast.dump(baseline_index, include_attributes=False) != ast.dump(
        expected_old_index, include_attributes=False
    ):
        raise RuntimeError("model baseline lookup is not the measured expression")

    removed_targets: list[str] = []
    added_targets: list[str] = []
    added_imports: list[str] = []
    for line in patch.unified_diff.splitlines():
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            content = line[1:]
            if "entities_pool[" in content and "hash(str(current_tick) + drive_name)" in content:
                removed_targets.append(content)
        elif line.startswith("+"):
            content = line[1:]
            if content.strip() == "import hashlib":
                added_imports.append(content)
            elif "entities_pool[" in content and "hashlib.sha256" in content:
                added_targets.append(content)
    if len(removed_targets) != 1 or len(added_targets) != 1 or len(added_imports) != 1:
        raise RuntimeError("model diff does not contain the bounded two-line replacement")
    if any(
        line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
        and line[1:].strip() not in {"import hashlib"}
        and "entities_pool[" not in line[1:]
        for line in patch.unified_diff.splitlines()
    ):
        raise RuntimeError("model diff contains an unrelated changed line")
    removed_parts = _target_line_parts(removed_targets[0])
    added_parts = _target_line_parts(added_targets[0])
    if removed_parts is None or added_parts is None:
        raise RuntimeError("model diff target line shape is unsupported")
    removed_index = ast.parse(removed_parts[1], mode="eval").body
    if ast.dump(removed_index, include_attributes=False) != ast.dump(
        baseline_index, include_attributes=False
    ):
        raise RuntimeError("model diff removes a different lookup")
    candidate_index_text = added_parts[1].strip()
    candidate_index = ast.parse(candidate_index_text, mode="eval").body
    if not isinstance(candidate_index, ast.BinOp) or not isinstance(
        candidate_index.op, ast.Mod
    ):
        raise RuntimeError("model diff replacement has no bounded pool index")
    expected_new_index = _approved_index_left()
    if ast.dump(candidate_index.left, include_attributes=False) != ast.dump(
        expected_new_index, include_attributes=False
    ):
        raise RuntimeError("model diff replacement is not the approved deterministic expression")
    if not (
        isinstance(candidate_index.right, ast.Call)
        and isinstance(candidate_index.right.func, ast.Name)
        and candidate_index.right.func.id == "len"
        and len(candidate_index.right.args) == 1
        and isinstance(candidate_index.right.args[0], ast.Name)
        and candidate_index.right.args[0].id == "entities_pool"
    ):
        raise RuntimeError("model diff replacement has an unexpected pool bound")

    # Replace only the index expression and add the model-requested import;
    # preserve every other baseline byte, including a non-ASCII fallback
    # string that an API response may have rendered incorrectly.
    after_lines = list(before_lines)
    original_line = before_lines[target_indices[0]]
    line_ending = "\n" if original_line.endswith("\n") else ""
    prefix, _old_index, suffix = baseline_parts
    after_lines[target_indices[0]] = prefix + candidate_index_text + suffix + line_ending
    logging_indices = [
        index for index, line in enumerate(after_lines) if line.strip() == "import logging"
    ]
    if len(logging_indices) != 1:
        raise RuntimeError("model baseline import anchor is ambiguous")
    import_line = after_lines[logging_indices[0]]
    import_ending = "\n" if import_line.endswith("\n") else ""
    after_lines.insert(logging_indices[0] + 1, "import hashlib" + import_ending)
    canonical_diff = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="a/" + TARGET_SCOPE,
            tofile="b/" + TARGET_SCOPE,
            n=3,
            lineterm="\n",
        )
    )
    canonical = validate_model_patch(
        {
            "title": patch.title,
            "scope": patch.scope,
            "hypothesis": patch.hypothesis,
            "unified_diff": canonical_diff,
        }
    )
    return replace(canonical, source_diff_hash=patch.model_response_hash)


def _diff_for_target(baseline: Path, candidate: Path) -> str:
    """Return the deterministic approved-file diff for authorization binding."""

    before = (baseline / TARGET_SCOPE).read_text(encoding="utf-8").splitlines(keepends=True)
    after = (candidate / TARGET_SCOPE).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile="a/" + TARGET_SCOPE,
            tofile="b/" + TARGET_SCOPE,
            n=3,
            lineterm="\n",
        )
    )


def _apply_unified_diff(
    candidate: Path, patch: ModelPatch, *, baseline: Path | None = None
) -> None:
    """Apply only the validated diff through the local Git CLI."""

    before = _tree_manifest(candidate)
    patch_file = candidate.parent / ".p7-patch-input.diff"
    try:
        patch_file.write_text(patch.unified_diff, encoding="utf-8", newline="\n")
        env = _safe_child_environment(root=candidate)
        for check in (True, False):
            command = ["git", "apply", "--ignore-whitespace"]
            if check:
                command.append("--check")
            command.append(str(patch_file))
            completed = subprocess.run(
                command,
                cwd=str(candidate),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("validated model patch could not be applied")
        after = _tree_manifest(candidate)
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        if changed != {TARGET_SCOPE}:
            raise RuntimeError("model patch changed a file outside the approved scope")
        source = (candidate / TARGET_SCOPE).read_text(encoding="utf-8")
        ast.parse(source, filename=TARGET_SCOPE)
        if re.search(r"\bhash\s*\(", source) and "hash(str(current_tick) + drive_name)" in source:
            raise RuntimeError("candidate still contains the process-randomized entity hash")
        if "hashlib" not in source:
            raise RuntimeError("candidate does not import the deterministic digest library")
        if baseline is not None:
            _assert_low_risk_transform(baseline, candidate)
    finally:
        try:
            patch_file.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically write a bounded JSON record without following aliases.

    A fixed ``.tmp`` sibling is vulnerable to another same-user process
    pre-creating a symlink/hard link at that name.  ``mkstemp`` gives this
    write its own O_EXCL descriptor; all link/regular-file checks are made on
    the descriptor as well as the directory entry before replacement.
    """

    target = Path(path)
    parent = target.parent
    if _is_link_like(parent) or not parent.is_dir():
        raise ValueError("JSON record parent is unsafe")
    if _lexists(target):
        if _is_link_like(target):
            raise ValueError("JSON record target is unsafe")
        try:
            target_stat = target.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("JSON record target is unavailable") from exc
        if not stat.S_ISREG(target_stat.st_mode) or getattr(target_stat, "st_nlink", 1) > 1:
            raise ValueError("JSON record target is unsafe")
    payload = (_canonical(value) + "\n").encode("utf-8")
    fd: int | None = None
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent)
        )
        temporary_path = Path(temporary_name)
        stream_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(stream_stat.st_mode)
            or getattr(stream_stat, "st_nlink", 1) != 1
            or _is_link_like(temporary_path)
        ):
            raise ValueError("JSON record temporary file is unsafe")
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            written = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(written.st_mode)
                or getattr(written, "st_nlink", 1) != 1
                or getattr(written, "st_ino", 0)
                != getattr(stream_stat, "st_ino", 0)
            ):
                raise ValueError("JSON record temporary file changed while writing")
        if not _lexists(temporary_path) or _is_link_like(temporary_path):
            raise ValueError("JSON record temporary file changed before replace")
        temporary_stat = temporary_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(temporary_stat.st_mode)
            or getattr(temporary_stat, "st_nlink", 1) != 1
            or getattr(temporary_stat, "st_ino", 0)
            != getattr(stream_stat, "st_ino", 0)
        ):
            raise ValueError("JSON record temporary file changed before replace")
        # Recheck the destination immediately before replace.  A race after
        # this point is still handled by replacing the directory entry (never
        # following its contents), while a pre-existing alias is rejected.
        if _lexists(target):
            if _is_link_like(target):
                raise ValueError("JSON record target changed to an unsafe entry")
            target_stat = target.stat(follow_symlinks=False)
            if not stat.S_ISREG(target_stat.st_mode) or getattr(target_stat, "st_nlink", 1) > 1:
                raise ValueError("JSON record target changed to an unsafe entry")
        os.replace(temporary_path, target)
        temporary_path = None
        # A directory fsync is supported on POSIX.  Windows may reject the
        # handle; the atomically replaced file is still the authoritative
        # record there, so keep that platform-specific limitation explicit.
        if os.name != "nt":
            directory_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _read_host_epoch() -> dict[str, Any]:
    """Read a fail-closed Windows host epoch without using wall-clock time."""

    unavailable = "orphan_recovery_host_epoch_unavailable"
    if os.name != "nt" or sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError(unavailable)
    try:
        version = sys.getwindowsversion()
        if int(version.major) < 10:
            raise ValueError(unavailable)

        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            machine_guid = winreg.QueryValueEx(key, "MachineGuid")[0]
        if not isinstance(machine_guid, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            machine_guid,
        ):
            raise ValueError(unavailable)
        parsed_machine_guid = uuid.UUID(machine_guid)
        if parsed_machine_guid.int == 0:
            raise ValueError(unavailable)
        machine_sha256 = hashlib.sha256(
            str(parsed_machine_guid).encode("ascii")
        ).hexdigest()

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        read_process_memory = kernel32.ReadProcessMemory
        read_process_memory.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        read_process_memory.restype = ctypes.c_int
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p

        def read_boot_id() -> int:
            value = ctypes.c_uint32()
            copied = ctypes.c_size_t()
            if not read_process_memory(
                get_current_process(),
                ctypes.c_void_p(0x7FFE02C4),
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(copied),
            ) or copied.value != 4:
                raise ValueError(unavailable)
            boot_id = int(value.value)
            if not 0 < boot_id <= 0xFFFFFFFF:
                raise ValueError(unavailable)
            return boot_id

        before_boot_id = read_boot_id()

        # Microsoft documents this counter as starting at zero, accumulating
        # awake running time, excluding sleep/hibernate, and ignoring wall-
        # clock adjustments.  It supplements BootId/DbgHiberBoot; neither of
        # those fields alone is treated as proof of a full reboot.
        # https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-queryunbiasedinterrupttime
        query_unbiased_interrupt_time = kernel32.QueryUnbiasedInterruptTime
        query_unbiased_interrupt_time.argtypes = [
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        query_unbiased_interrupt_time.restype = ctypes.c_int
        unbiased_interrupt_time = ctypes.c_ulonglong()
        if not query_unbiased_interrupt_time(ctypes.byref(unbiased_interrupt_time)):
            raise ValueError(unavailable)
        unbiased_interrupt_100ns = int(unbiased_interrupt_time.value)
        if not 0 < unbiased_interrupt_100ns <= 2**64 - 1:
            raise ValueError(unavailable)

        ntdll = ctypes.WinDLL("ntdll")
        query_system_information = ntdll.NtQuerySystemInformation
        query_system_information.argtypes = [
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        query_system_information.restype = ctypes.c_long
        class _SystemBootEnvironmentInformation(ctypes.Structure):
            _fields_ = [
                ("BootIdentifier", ctypes.c_ubyte * 16),
                ("FirmwareType", ctypes.c_uint32),
                ("BootFlags", ctypes.c_uint64),
            ]

        if (
            ctypes.sizeof(_SystemBootEnvironmentInformation) != 32
            or _SystemBootEnvironmentInformation.BootFlags.offset != 24
        ):
            raise ValueError(unavailable)
        information = _SystemBootEnvironmentInformation()
        returned = ctypes.c_ulong()
        status = query_system_information(
            90,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        )
        if status != 0 or returned.value != 32:
            raise ValueError(unavailable)
        boot_identifier = bytes(information.BootIdentifier)
        if not any(boot_identifier):
            raise ValueError(unavailable)
        firmware_type = int(information.FirmwareType)
        if firmware_type not in (1, 2):
            raise ValueError(unavailable)
        boot_flags = int(information.BootFlags)
        hiberboot = bool(boot_flags & 0x2)

        after_boot_id = read_boot_id()
        if before_boot_id != after_boot_id:
            raise ValueError(unavailable)
        return {
            "provider": "windows-kuser-v1",
            "machine_sha256": machine_sha256,
            "boot_id": after_boot_id,
            "hiberboot": hiberboot,
            "unbiased_interrupt_100ns": unbiased_interrupt_100ns,
        }
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) == unavailable:
            raise
        raise ValueError(unavailable) from None


_ORPHAN_LEASE_PROTOCOL = "p7-orphan-lease-v1"
_ORPHAN_LEASE_DIRECTORY = ".brain-memory-p7-orphan-leases"
_ORPHAN_LEASE_LOCK = ".registry.lock"
_ORPHAN_LEASE_FILE_RE = re.compile(r"^[0-9a-f]{32,128}\.json$")
_ORPHAN_LEASE_MAX_FILES = 1024
_ORPHAN_LEASE_MAX_BYTES = 16 * 1024
_ORPHAN_LEASE_TTL_SEC = 180
_ORPHAN_LEASE_GRACE_SEC = 60
_ORPHAN_LEASE_UNSIGNED_FIELDS = frozenset(
    {
        "schema_version",
        "protocol",
        "namespace_sha256",
        "operation",
        "run_id",
        "nonce",
        "name",
        "image",
        "context",
        "docker_cli",
        "docker_cli_sha256",
        "endpoint_sha256",
        "created_at",
        "expires_at",
        "state",
        "process_stopped",
        "sequence",
        "cleaned_at",
    }
)
_ORPHAN_RECOVERY_PROTOCOL = "p7-orphan-recovery-v1"
_ORPHAN_RECOVERY_MAX_ENTRIES = 16
_ORPHAN_RECOVERY_MAX_BYTES = 2 * 1024 * 1024
_ORPHAN_RECOVERY_EPOCH_FIELDS = frozenset(
    {"provider", "machine_sha256", "boot_id", "hiberboot", "unbiased_interrupt_100ns"}
)


@dataclass(frozen=True, slots=True)
class OrphanLeaseHandle:
    """Opaque handle for one host-owned Docker operation lease."""

    path: Path
    nonce: str
    operation: str
    endpoint_digest: str


class OrphanLeaseStore:
    """Durable, host-only registry for Docker operations which may outlive us.

    The registry is intentionally separate from ``RunLayout``.  It contains
    only enough authenticated ownership data to perform a narrowly scoped
    nonce reaper after a process restart.  The manifest key is supplied by the
    caller and is never written to disk.  Invalid or ambiguous records are
    retained and cause a fail-closed status; this class never performs a
    broad Docker cleanup.
    """

    def __init__(
        self,
        root: Path | str,
        master_key: bytes | bytearray | str,
        *,
        namespace: Path | str | None = None,
        persistent: bool = True,
    ) -> None:
        base = Path(root).expanduser().resolve(strict=True)
        if _is_link_like(base) or not base.is_dir():
            raise ValueError("orphan lease root is unsafe")
        namespace_root = Path(namespace or base).expanduser().resolve(strict=True)
        if _is_link_like(namespace_root) or not namespace_root.is_dir():
            raise ValueError("orphan lease namespace is unsafe")
        self.root = base
        self._key = _coerce_manifest_key(master_key)
        self.persistent = bool(persistent)

        def namespace_text(path: Path) -> str:
            rendered = str(path)
            if os.name == "nt":
                return os.path.normcase(rendered).replace("\\", "/")
            # Backslashes and case are meaningful in POSIX path components.
            return rendered

        # Bind records to both the shared run root and the repository identity
        # without persisting either absolute path.  Distinct projects using
        # the same OS temp directory therefore cannot share a registry.
        self._namespace_digest = _digest(
            _canonical(
                {
                    "registry_root": namespace_text(base),
                    "project_root": namespace_text(namespace_root),
                }
            )
        )

    @property
    def directory(self) -> Path:
        return self.root / (
            _ORPHAN_LEASE_DIRECTORY + "-" + self._namespace_digest[:16]
        )

    @property
    def lock_path(self) -> Path:
        return self.directory / _ORPHAN_LEASE_LOCK

    @property
    def recovery_path(self) -> Path:
        return self.root / (
            ".brain-memory-p7-recovery-" + self._namespace_digest[:16] + ".json"
        )

    def _recovery_mac(self, payload: Mapping[str, Any]) -> str:
        material = (
            "brain-memory-p7-recovery-v1:" + self._namespace_digest
        ).encode("utf-8")
        key = hmac.new(self._key, material, hashlib.sha256).digest()
        return hmac.new(key, _canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_recovery_epoch(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _ORPHAN_RECOVERY_EPOCH_FIELDS:
            raise ValueError("orphan recovery epoch is invalid")
        if (
            value["provider"] != "windows-kuser-v1"
            or not isinstance(value["machine_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["machine_sha256"])
            or not isinstance(value["boot_id"], int)
            or isinstance(value["boot_id"], bool)
            or not 1 <= value["boot_id"] <= 2**32 - 1
            or not isinstance(value["hiberboot"], bool)
            or not isinstance(value["unbiased_interrupt_100ns"], int)
            or isinstance(value["unbiased_interrupt_100ns"], bool)
            or not 1 <= value["unbiased_interrupt_100ns"] <= 2**64 - 1
        ):
            raise ValueError("orphan recovery epoch is invalid")
        return dict(value)

    @staticmethod
    def _read_bounded_json(path: Path, *, max_bytes: int, label: str = "orphan recovery ledger") -> Any:
        if _is_link_like(path) or not path.is_file():
            raise ValueError(label + " is unsafe")
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)) | int(
                getattr(os, "O_BINARY", 0)
            )
            descriptor = os.open(os.fspath(path), flags)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or getattr(before, "st_nlink", 1) != 1
                or before.st_size > max_bytes
            ):
                raise ValueError(label + " is unsafe")
            data = bytearray()
            while len(data) <= max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
            if (
                len(data) > max_bytes
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
            ):
                raise ValueError(label + " changed while being read")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(label + " unavailable") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        try:
            return json.loads(bytes(data).decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(label + " is invalid") from exc

    def _validate_recovery_ledger(
        self, value: Any, records: list[tuple[Path, dict[str, Any]]]
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "protocol", "namespace_sha256", "entries", "mac"
        }:
            raise ValueError("orphan recovery ledger is invalid")
        unsigned = {key: value[key] for key in value if key != "mac"}
        if (
            type(unsigned["schema_version"]) is not int
            or unsigned["schema_version"] != 1
            or unsigned["protocol"] != _ORPHAN_RECOVERY_PROTOCOL
            or unsigned["namespace_sha256"] != self._namespace_digest
            or not isinstance(unsigned["entries"], list)
            or len(unsigned["entries"]) > _ORPHAN_RECOVERY_MAX_ENTRIES
            or not isinstance(value["mac"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["mac"])
            or not hmac.compare_digest(value["mac"], self._recovery_mac(unsigned))
        ):
            raise ValueError("orphan recovery ledger is invalid")
        current_digests = {
            payload["nonce"]: _digest(_canonical(payload)) for _path, payload in records
        }
        seen_ids: set[str] = set()
        pending_index: int | None = None
        validated: list[dict[str, Any]] = []
        for index, entry in enumerate(unsigned["entries"]):
            if not isinstance(entry, dict) or set(entry) != {"checkpoint", "receipt"}:
                raise ValueError("orphan recovery ledger is invalid")
            checkpoint = entry["checkpoint"]
            if not isinstance(checkpoint, dict) or set(checkpoint) != {
                "recovery_id", "host_epoch", "records", "targets"
            }:
                raise ValueError("orphan recovery ledger is invalid")
            recovery_id = checkpoint["recovery_id"]
            if not isinstance(recovery_id, str) or not re.fullmatch(r"[0-9a-f]{32}", recovery_id):
                raise ValueError("orphan recovery ledger is invalid")
            if recovery_id in seen_ids:
                raise ValueError("orphan recovery ledger is invalid")
            seen_ids.add(recovery_id)
            self._validate_recovery_epoch(checkpoint["host_epoch"])
            snapshot = checkpoint["records"]
            targets = checkpoint["targets"]
            if (
                not isinstance(snapshot, dict)
                or not snapshot
                or len(snapshot) > _ORPHAN_LEASE_MAX_FILES
                or any(
                    not isinstance(nonce, str)
                    or not re.fullmatch(r"[0-9a-f]{32,128}", nonce)
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for nonce, digest in snapshot.items()
                )
                or not isinstance(targets, list)
                or len(targets) > len(snapshot)
                or targets != sorted(set(targets))
                or any(target not in snapshot for target in targets)
            ):
                raise ValueError("orphan recovery ledger is invalid")
            receipt = entry["receipt"]
            if receipt is None:
                if index == len(unsigned["entries"]) - 1:
                    pending_index = index
            else:
                if not isinstance(receipt, dict) or set(receipt) != {
                    "checkpoint_sha256", "host_epoch", "containers_absent"
                }:
                    raise ValueError("orphan recovery ledger is invalid")
                receipt_epoch = self._validate_recovery_epoch(receipt["host_epoch"])
                if (
                    not isinstance(receipt["checkpoint_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt["checkpoint_sha256"])
                    or receipt["checkpoint_sha256"] != _digest(_canonical(checkpoint))
                    or receipt_epoch["provider"] != checkpoint["host_epoch"]["provider"]
                    or receipt_epoch["machine_sha256"] != checkpoint["host_epoch"]["machine_sha256"]
                    or receipt_epoch["boot_id"] <= checkpoint["host_epoch"]["boot_id"]
                    or receipt_epoch["unbiased_interrupt_100ns"] >= checkpoint["host_epoch"]["unbiased_interrupt_100ns"]
                    or receipt_epoch["hiberboot"] is not False
                    or receipt["containers_absent"] is not True
                ):
                    raise ValueError("orphan recovery ledger is invalid")
                if any(current_digests.get(target) != snapshot[target] for target in targets):
                    raise ValueError("orphan recovery ledger is invalid")
            validated.append({"checkpoint": checkpoint, "receipt": receipt})
        return {
            "entries": validated,
            "pending_index": pending_index,
            "current_digests": current_digests,
        }

    def _load_recovery_ledger(
        self,
        records: list[tuple[Path, dict[str, Any]]],
        *,
        verify_epoch: bool = False,
    ) -> dict[str, Any] | None:
        if not _lexists(self.recovery_path):
            return None
        parent = self.recovery_path.parent
        if _is_link_like(parent) or not parent.is_dir():
            raise ValueError("orphan recovery ledger is unsafe")
        raw = self._read_bounded_json(self.recovery_path, max_bytes=_ORPHAN_RECOVERY_MAX_BYTES)
        ledger = self._validate_recovery_ledger(raw, records)
        if verify_epoch:
            try:
                current = self._validate_recovery_epoch(_read_host_epoch())
            except Exception as exc:
                raise ValueError("orphan_recovery_host_epoch_unavailable") from exc
            for entry in ledger["entries"]:
                receipt = entry["receipt"]
                if receipt is None:
                    continue
                epoch = receipt["host_epoch"]
                if (
                    current["provider"] != epoch["provider"]
                    or current["machine_sha256"] != epoch["machine_sha256"]
                    or current["boot_id"] < epoch["boot_id"]
                ):
                    raise ValueError("orphan_recovery_host_mismatch")
        return ledger

    def _recovery_ledger_value(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        unsigned = {
            "schema_version": 1,
            "protocol": _ORPHAN_RECOVERY_PROTOCOL,
            "namespace_sha256": self._namespace_digest,
            "entries": entries,
        }
        value = dict(unsigned)
        value["mac"] = self._recovery_mac(unsigned)
        if len((_canonical(value) + "\n").encode("utf-8")) > _ORPHAN_RECOVERY_MAX_BYTES:
            raise ValueError("orphan recovery ledger exceeds its bound")
        return value

    @staticmethod
    def _recovery_result(
        phase: str, recovery_id: str = "", count: int = 0, reason: str = ""
    ) -> dict[str, Any]:
        result = {
            "phase": phase,
            "recovery_id": recovery_id,
            "affected_count": int(count),
            "reason": reason,
        }
        return result

    def prepare_recovery(self, *, now: int | None = None) -> dict[str, Any]:
        if not self.persistent:
            return self._recovery_result("blocked", reason="orphan_lease_key_unavailable")
        if now is not None and (not isinstance(now, int) or isinstance(now, bool) or now < 0):
            return self._recovery_result("blocked", reason="orphan_recovery_invalid")
        try:
            directory = self._ensure_directory(create=False)
            if directory is None:
                return self._recovery_result("blocked", reason="orphan_recovery_registry_unavailable")
            current = int(time.time()) if now is None else now
            with _authorization_file_lock(self.lock_path):
                records = self._records(directory)
                ledger = self._load_recovery_ledger(records)
                if ledger is not None and ledger["pending_index"] is not None:
                    entry = ledger["entries"][ledger["pending_index"]]
                    if entry["checkpoint"]["records"] == ledger["current_digests"]:
                        try:
                            current_epoch = self._validate_recovery_epoch(_read_host_epoch())
                        except Exception:
                            return self._recovery_result(
                                "blocked", reason="orphan_recovery_host_epoch_unavailable"
                            )
                        before_epoch = entry["checkpoint"]["host_epoch"]
                        stale_epoch = (
                            current_epoch["provider"] == before_epoch["provider"]
                            and current_epoch["machine_sha256"] == before_epoch["machine_sha256"]
                            and current_epoch["boot_id"] > before_epoch["boot_id"]
                            and current_epoch["unbiased_interrupt_100ns"] >= before_epoch["unbiased_interrupt_100ns"]
                        )
                        if not stale_epoch:
                            return self._recovery_result(
                                "recovery_pending",
                                entry["checkpoint"]["recovery_id"],
                                len(entry["checkpoint"]["targets"]),
                                "orphan_recovery_reboot_required",
                            )
                recovered = {
                    target
                    for entry in (ledger["entries"] if ledger else [])
                    if entry["receipt"] is not None
                    for target in entry["checkpoint"]["targets"]
                }
                targets = [
                    payload["nonce"]
                    for _path, payload in records
                    if payload["nonce"] not in recovered
                    and payload["state"] != "cleaned"
                    and current > int(payload["expires_at"]) + _ORPHAN_LEASE_GRACE_SEC
                    and payload["process_stopped"] is not True
                ]
                if any(
                    payload["state"] != "cleaned"
                    and current <= int(payload["expires_at"]) + _ORPHAN_LEASE_GRACE_SEC
                    for _path, payload in records
                ):
                    return self._recovery_result("blocked", reason="orphan_recovery_live")
                if not targets and (ledger is None or ledger["pending_index"] is None):
                    return self._recovery_result("no_recovery_needed", reason="orphan_recovery_not_needed")
                try:
                    epoch = self._validate_recovery_epoch(_read_host_epoch())
                except Exception:
                    return self._recovery_result("blocked", reason="orphan_recovery_host_epoch_unavailable")
                checkpoint = {
                    "recovery_id": secrets.token_hex(16),
                    "host_epoch": epoch,
                    "records": {
                        payload["nonce"]: _digest(_canonical(payload))
                        for _path, payload in records
                    },
                    "targets": sorted(set(targets)),
                }
                entries = list(ledger["entries"] if ledger else [])
                if len(entries) >= _ORPHAN_RECOVERY_MAX_ENTRIES:
                    return self._recovery_result("blocked", reason="orphan_recovery_ledger_full")
                entries.append({"checkpoint": checkpoint, "receipt": None})
                _write_json(self.recovery_path, self._recovery_ledger_value(entries))
                return self._recovery_result(
                    "recovery_pending", checkpoint["recovery_id"], len(targets),
                    "orphan_recovery_reboot_required",
                )
        except Exception:
            return self._recovery_result("blocked", reason="orphan_recovery_registry_invalid")

    def verify_recovery(self, recovery_id: str) -> dict[str, Any]:
        if not self.persistent:
            return self._recovery_result("blocked", reason="orphan_lease_key_unavailable")
        if not isinstance(recovery_id, str) or not re.fullmatch(r"[0-9a-f]{32}", recovery_id):
            return self._recovery_result("blocked", reason="orphan_recovery_invalid")
        try:
            directory = self._ensure_directory(create=False)
            if directory is None:
                return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_unavailable")
            with _authorization_file_lock(self.lock_path):
                records = self._records(directory)
                ledger = self._load_recovery_ledger(records)
                if ledger is None:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_invalid")
                start_digests = dict(ledger["current_digests"])
                selected_index, selected = next(
                    ((index, entry) for index, entry in enumerate(ledger["entries"])
                     if entry["checkpoint"]["recovery_id"] == recovery_id),
                    None,
                ) or (None, None)
                if selected is None or selected_index is None:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_not_found")
                if selected["receipt"] is None and selected_index != ledger["pending_index"]:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_checkpoint_superseded")
                if selected["receipt"] is None and selected["checkpoint"]["records"] != ledger["current_digests"]:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_changed")
                before = self._validate_recovery_epoch(selected["checkpoint"]["host_epoch"])
                try:
                    after = self._validate_recovery_epoch(_read_host_epoch())
                except Exception:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_host_epoch_unavailable")
                receipt = selected["receipt"]
                same_machine = (
                    after["provider"] == before["provider"]
                    and after["machine_sha256"] == before["machine_sha256"]
                )
                rebooted = (
                    same_machine
                    and after["boot_id"] > before["boot_id"]
                    and after["unbiased_interrupt_100ns"] < before["unbiased_interrupt_100ns"]
                )
                receipt_valid_now = receipt is not None and same_machine and after["boot_id"] >= receipt["host_epoch"]["boot_id"]
                if (receipt is None and (not rebooted or after["hiberboot"] is not False)) or (
                    receipt is not None and not receipt_valid_now
                ):
                    return self._recovery_result(
                        "blocked", recovery_id,
                        len(selected["checkpoint"]["targets"]),
                        "orphan_recovery_reboot_required"
                        if same_machine else "orphan_recovery_host_mismatch",
                    )
                by_nonce = {payload["nonce"]: payload for _path, payload in records}
                for nonce in selected["checkpoint"]["targets"]:
                    payload = by_nonce.get(nonce)
                    if payload is None or _digest(_canonical(payload)) != selected["checkpoint"]["records"].get(nonce):
                        return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_invalid")
                    if not _reap_owned_eval_container(
                        payload["docker_cli"], payload["context"], payload["nonce"], payload["image"],
                        name_prefix="brain-memory-p7-" + payload["operation"] + "-",
                        expected_endpoint_digest=payload["endpoint_sha256"],
                        expected_docker_cli_digest=payload["docker_cli_sha256"],
                        allow_removal=False,
                    ):
                        return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_resources_unverified")
                records_after = self._records(directory)
                if {
                    payload["nonce"]: _digest(_canonical(payload))
                    for _path, payload in records_after
                } != start_digests:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_invalid")
                try:
                    after_final = self._validate_recovery_epoch(_read_host_epoch())
                except Exception:
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_host_epoch_unavailable")
                if (
                    after_final["provider"] != after["provider"]
                    or after_final["machine_sha256"] != after["machine_sha256"]
                    or after_final["boot_id"] != after["boot_id"]
                    or after_final["unbiased_interrupt_100ns"] < after["unbiased_interrupt_100ns"]
                ):
                    return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_host_epoch_changed")
                if selected["receipt"] is None:
                    selected["receipt"] = {
                        "checkpoint_sha256": _digest(_canonical(selected["checkpoint"])),
                        "host_epoch": after,
                        "containers_absent": True,
                    }
                    entries = [
                        selected if entry["checkpoint"]["recovery_id"] == recovery_id else entry
                        for entry in ledger["entries"]
                    ]
                    _write_json(self.recovery_path, self._recovery_ledger_value(entries))
                return self._recovery_result(
                    "recovery_verified", recovery_id,
                    len(selected["checkpoint"]["targets"]), "",
                )
        except Exception:
            return self._recovery_result("blocked", recovery_id, reason="orphan_recovery_registry_invalid")

    def _ensure_directory(self, *, create: bool) -> Path | None:
        directory = self.directory
        legacy = self.root / _ORPHAN_LEASE_DIRECTORY
        if legacy != directory and _lexists(legacy):
            # The old shared namespace cannot be attributed to one repository
            # safely.  Never ignore or automatically migrate it.
            raise ValueError("legacy orphan lease registry requires review")
        if not _lexists(directory) and _lexists(self.recovery_path):
            raise ValueError("orphan recovery registry is unavailable")
        if _lexists(directory):
            if _is_link_like(directory) or not directory.is_dir():
                raise ValueError("orphan lease registry is unsafe")
            # A host registry must not be writable by group/others on POSIX.
            # Windows ACLs are governed by the parent profile and are not
            # represented by POSIX mode bits; link/reparse checks still apply.
            if os.name != "nt":
                info = directory.stat(follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if mode & 0o077 or (
                    hasattr(os, "getuid") and info.st_uid != os.getuid()
                ):
                    raise ValueError("orphan lease registry permissions are unsafe")
            return directory
        if not create:
            return None
        if _is_link_like(self.root):
            raise ValueError("orphan lease root is unsafe")
        directory.mkdir(mode=0o700, exist_ok=False)
        if _is_link_like(directory) or not directory.is_dir():
            raise ValueError("orphan lease registry is unsafe")
        if os.name != "nt":
            try:
                directory.chmod(0o700)
                info = directory.stat(follow_symlinks=False)
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise ValueError("orphan lease registry owner is unsafe")
            except OSError as exc:
                raise ValueError("orphan lease registry permissions are unavailable") from exc
        return directory

    @staticmethod
    def _mac_key(master_key: bytes, nonce: str) -> bytes:
        return hmac.new(
            master_key,
            ("brain-memory-p7-orphan-lease-v1:" + nonce).encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def _mac(self, payload: Mapping[str, Any]) -> str:
        nonce = str(payload.get("nonce", ""))
        return hmac.new(
            self._mac_key(self._key, nonce),
            _canonical(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _valid_int(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and minimum <= value <= maximum
        )

    def _validate_payload(
        self, payload: Any, *, expected_nonce: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("orphan lease record is invalid")
        if set(payload) != _ORPHAN_LEASE_UNSIGNED_FIELDS | {"mac"}:
            raise ValueError("orphan lease record is invalid")
        unsigned = {key: payload[key] for key in _ORPHAN_LEASE_UNSIGNED_FIELDS}
        if (
            unsigned["schema_version"] != HOST_SCHEMA_VERSION
            or unsigned["protocol"] != _ORPHAN_LEASE_PROTOCOL
            or unsigned["namespace_sha256"] != self._namespace_digest
            or unsigned["operation"] not in {"probe", "eval"}
        ):
            raise ValueError("orphan lease record is invalid")
        nonce = unsigned["nonce"]
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32,128}", nonce):
            raise ValueError("orphan lease record is invalid")
        if expected_nonce is not None and nonce != expected_nonce:
            raise ValueError("orphan lease record is invalid")
        # Authenticate the complete unsigned envelope before following the
        # operator-controlled Docker path or doing any other expensive I/O.
        # A forged record therefore cannot turn the registry into an arbitrary
        # file reader even when its JSON shape is otherwise plausible.
        supplied_mac = payload.get("mac")
        if not isinstance(supplied_mac, str) or not re.fullmatch(
            r"[0-9a-f]{64}", supplied_mac
        ):
            raise ValueError("orphan lease record is invalid")
        if not hmac.compare_digest(supplied_mac, self._mac(unsigned)):
            raise ValueError("orphan lease authentication failed")
        run_id = unsigned["run_id"]
        if run_id not in ("",) and (
            not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{16}", run_id)
        ):
            raise ValueError("orphan lease record is invalid")
        operation = str(unsigned["operation"])
        prefix = "brain-memory-p7-" + operation + "-"
        name = unsigned["name"]
        if name != prefix + nonce[:32] or not re.fullmatch(
            r"brain-memory-p7-(?:probe|eval)-[0-9a-f]{32}", name
        ):
            raise ValueError("orphan lease record is invalid")
        image = unsigned["image"]
        if not isinstance(image, str) or not _DOCKER_IMAGE_RE.fullmatch(image):
            raise ValueError("orphan lease record is invalid")
        context = unsigned["context"]
        if not isinstance(context, str) or not _DOCKER_CONTEXT_RE.fullmatch(context):
            raise ValueError("orphan lease record is invalid")
        docker_cli = _validate_docker_cli(unsigned["docker_cli"])
        if docker_cli != unsigned["docker_cli"]:
            raise ValueError("orphan lease record is invalid")
        cli_digest = unsigned["docker_cli_sha256"]
        if not isinstance(cli_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", cli_digest):
            raise ValueError("orphan lease record is invalid")
        try:
            observed_cli_digest = _bounded_file_digest(
                docker_cli, max_bytes=_DOCKER_CLI_MAX_BYTES
            )
        except (OSError, ValueError):
            raise ValueError("orphan lease record is invalid")
        if observed_cli_digest != cli_digest:
            raise ValueError("orphan lease record is invalid")
        endpoint_digest = unsigned["endpoint_sha256"]
        if not isinstance(endpoint_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", endpoint_digest
        ):
            raise ValueError("orphan lease record is invalid")
        created_at = unsigned["created_at"]
        expires_at = unsigned["expires_at"]
        if (
            not self._valid_int(created_at)
            or not self._valid_int(expires_at)
            or expires_at <= created_at
            or expires_at - created_at > 24 * 60 * 60
        ):
            raise ValueError("orphan lease record is invalid")
        state = unsigned["state"]
        if state not in {"armed", "cleanup_pending", "cleaned"}:
            raise ValueError("orphan lease record is invalid")
        if not isinstance(unsigned["process_stopped"], bool):
            raise ValueError("orphan lease record is invalid")
        # ``process_stopped`` is a one-way safety fact.  An armed lease can
        # never claim that its creator already stopped; that fact may only be
        # recorded while entering/remaining in ``cleanup_pending``.  Keeping
        # this invariant at the authenticated-record boundary also rejects
        # pre-existing or hand-crafted envelopes which would let ``complete``
        # skip the pending phase.
        if state == "armed" and unsigned["process_stopped"] is not False:
            raise ValueError("orphan lease record is invalid")
        if state == "cleaned" and unsigned["process_stopped"] is not True:
            raise ValueError("orphan lease record is invalid")
        if not self._valid_int(unsigned["sequence"], minimum=1, maximum=1_000_000):
            raise ValueError("orphan lease record is invalid")
        cleaned_at = unsigned["cleaned_at"]
        if cleaned_at is not None and (
            not self._valid_int(cleaned_at) or cleaned_at < created_at
        ):
            raise ValueError("orphan lease record is invalid")
        if state == "cleaned" and cleaned_at is None:
            raise ValueError("orphan lease record is invalid")
        if state != "cleaned" and cleaned_at is not None:
            raise ValueError("orphan lease record is invalid")
        return dict(unsigned)

    def _read_record(self, path: Path) -> dict[str, Any]:
        payload = self._read_bounded_json(
            path, max_bytes=_ORPHAN_LEASE_MAX_BYTES, label="orphan lease record"
        )
        return self._validate_payload(payload, expected_nonce=path.stem)

    def _records(self, directory: Path) -> list[tuple[Path, dict[str, Any]]]:
        try:
            entries: list[Path] = []
            with os.scandir(directory) as scan:
                for index, entry in enumerate(scan):
                    if index > _ORPHAN_LEASE_MAX_FILES:
                        raise ValueError("orphan lease registry exceeds its bound")
                    entries.append(Path(entry.path))
            entries.sort(key=lambda item: item.name)
        except OSError as exc:
            raise ValueError("orphan lease registry is unavailable") from exc
        records: list[tuple[Path, dict[str, Any]]] = []
        for entry in entries:
            if entry.name == _ORPHAN_LEASE_LOCK:
                continue
            if not _ORPHAN_LEASE_FILE_RE.fullmatch(entry.name):
                raise ValueError("orphan lease registry contains an unknown entry")
            records.append((entry, self._read_record(entry)))
        if len(records) > _ORPHAN_LEASE_MAX_FILES:
            raise ValueError("orphan lease registry exceeds its bound")
        return records

    def inspect(self, *, now: int | None = None) -> dict[str, Any]:
        """Read and aggregate authenticated leases without acquiring a lock."""

        if not self.persistent:
            return {
                "inspection_verified": False,
                "reason": "orphan_lease_key_unavailable",
            }
        if now is not None and (
            not isinstance(now, int) or isinstance(now, bool) or now < 0
        ):
            return {
                "inspection_verified": False,
                "reason": "orphan_lease_registry_invalid",
            }
        try:
            current = int(time.time()) if now is None else now
            if not self._valid_int(current):
                return {
                    "inspection_verified": False,
                    "reason": "orphan_lease_registry_invalid",
                }
            directory = self._ensure_directory(create=False)
            records = [] if directory is None else self._records(directory)
            ledger = None if directory is None else self._load_recovery_ledger(records, verify_epoch=True)
            if ledger is not None and ledger["pending_index"] is not None:
                pending = ledger["entries"][ledger["pending_index"]]
                if pending["checkpoint"]["records"] != ledger["current_digests"]:
                    raise ValueError("orphan recovery registry changed")
            counts = {
                "inspection_verified": True,
                "reason": "",
                "record_count": len(records),
                "live_waiting_count": 0,
                "expired_stop_unverified_count": 0,
                "stopped_pending_cleanup_count": 0,
                "cleaned_retained_count": 0,
            }
            if ledger is not None:
                counts["recovery_pending"] = ledger["pending_index"] is not None
                counts["recovery_checkpoint_count"] = len(ledger["entries"])
                counts["recovered_retained_count"] = sum(
                    len(entry["checkpoint"]["targets"])
                    for entry in ledger["entries"]
                    if entry["receipt"] is not None
                )
            recovered = {
                target
                for entry in (ledger["entries"] if ledger else [])
                if entry["receipt"] is not None
                for target in entry["checkpoint"]["targets"]
            }
            for _path, payload in records:
                if payload["state"] == "cleaned":
                    counts["cleaned_retained_count"] += 1
                elif payload["nonce"] in recovered:
                    continue
                elif current <= int(payload["expires_at"]) + _ORPHAN_LEASE_GRACE_SEC:
                    counts["live_waiting_count"] += 1
                elif payload["process_stopped"] is True:
                    counts["stopped_pending_cleanup_count"] += 1
                else:
                    counts["expired_stop_unverified_count"] += 1
            return counts
        except ValueError as exc:
            reason = str(exc)
            if reason not in {
                "orphan_recovery_host_epoch_unavailable",
                "orphan_recovery_host_mismatch",
                "orphan recovery registry is unavailable",
                "orphan recovery registry changed",
            }:
                reason = "orphan_lease_registry_invalid"
            return {
                "inspection_verified": False,
                "reason": reason,
            }
        except Exception:
            return {
                "inspection_verified": False,
                "reason": "orphan_lease_registry_invalid",
            }

    def _write_transition(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        state: str,
        now: int,
        process_stopped: bool | None = None,
    ) -> None:
        updated = dict(payload)
        previous_state = payload.get("state")
        allowed_predecessors = {
            "cleanup_pending": {"armed", "cleanup_pending"},
            "cleaned": {"cleanup_pending"},
        }
        if previous_state not in allowed_predecessors.get(state, set()):
            raise ValueError("orphan lease transition is invalid")
        previous_stopped = payload.get("process_stopped")
        if not isinstance(previous_stopped, bool):
            raise ValueError("orphan lease transition is invalid")
        if (
            previous_stopped
            and process_stopped is not None
            and process_stopped is not True
        ):
            # Never downgrade a durable stop observation.  Returning an error
            # leaves the stronger fact intact for a later authenticated sweep.
            raise ValueError("orphan lease process stop cannot be downgraded")
        sequence = int(payload["sequence"])
        if sequence >= 1_000_000:
            raise ValueError("orphan lease transition limit reached")
        updated["state"] = state
        updated["sequence"] = sequence + 1
        updated["cleaned_at"] = now if state == "cleaned" else None
        if process_stopped is not None:
            updated["process_stopped"] = bool(process_stopped)
        if state == "cleaned" and updated["process_stopped"] is not True:
            raise ValueError("orphan lease transition is invalid")
        record = dict(updated)
        record["mac"] = self._mac(updated)
        _write_json(path, record)

    def arm(
        self,
        *,
        operation: str,
        nonce: str,
        name: str,
        image: str,
        context: str,
        docker_cli: str,
        endpoint_digest: str,
        run_id: str = "",
        ttl_sec: int = _ORPHAN_LEASE_TTL_SEC,
        now: int | None = None,
    ) -> OrphanLeaseHandle:
        if not self.persistent:
            raise RuntimeError("orphan lease key is not restart-durable")
        operation = str(operation or "").lower()
        nonce = str(nonce or "").lower()
        docker = _validate_docker_cli(docker_cli)
        try:
            ttl_value = int(ttl_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("orphan lease arguments are invalid") from exc
        if (
            operation not in {"probe", "eval"}
            or not re.fullmatch(r"[0-9a-f]{32,128}", nonce)
            or name != "brain-memory-p7-" + operation + "-" + nonce[:32]
            or not _DOCKER_IMAGE_RE.fullmatch(str(image or ""))
            or not _DOCKER_CONTEXT_RE.fullmatch(str(context or ""))
            or not docker
            or not re.fullmatch(r"[0-9a-f]{64}", str(endpoint_digest or ""))
            or (run_id and not re.fullmatch(r"[0-9a-f]{16}", str(run_id)))
            or not isinstance(ttl_sec, int)
            or isinstance(ttl_sec, bool)
            or not self._valid_int(ttl_value, minimum=30, maximum=24 * 60 * 60)
        ):
            raise ValueError("orphan lease arguments are invalid")
        if now is not None and (
            not isinstance(now, int) or isinstance(now, bool)
        ):
            raise ValueError("orphan lease time is invalid")
        created = int(time.time()) if now is None else now
        if not self._valid_int(created):
            raise ValueError("orphan lease time is invalid")
        expires = created + ttl_value
        if not self._valid_int(expires):
            raise ValueError("orphan lease time is invalid")
        preflight = self.sweep(now=created)
        if not bool(preflight.get("ready")):
            raise RuntimeError("orphan lease cleanup is pending")
        directory = self._ensure_directory(create=True)
        assert directory is not None
        path = directory / (nonce + ".json")
        with _authorization_file_lock(self.lock_path):
            if _lexists(path):
                raise ValueError("orphan lease nonce is already registered")
            current_records = self._records(directory)
            recovery_ledger = self._load_recovery_ledger(current_records, verify_epoch=True)
            if recovery_ledger is not None and recovery_ledger["pending_index"] is not None:
                pending = recovery_ledger["entries"][recovery_ledger["pending_index"]]
                if pending["checkpoint"]["records"] != {
                    payload["nonce"]: _digest(_canonical(payload))
                    for _path, payload in current_records
                }:
                    raise RuntimeError("orphan recovery registry changed")
                raise RuntimeError("orphan recovery checkpoint is pending")
            recovered = {
                target
                for entry in (recovery_ledger["entries"] if recovery_ledger else [])
                if entry["receipt"] is not None
                for target in entry["checkpoint"]["targets"]
            }
            # Do not arm a second operation while an earlier operation is
            # still live or awaiting cleanup.  Cleaned tombstones are safe.
            for _other_path, other in current_records:
                if other["state"] != "cleaned" and other["nonce"] not in recovered:
                    raise RuntimeError("orphan lease cleanup is pending")
            unsigned = {
                "schema_version": HOST_SCHEMA_VERSION,
                "protocol": _ORPHAN_LEASE_PROTOCOL,
                "namespace_sha256": self._namespace_digest,
                "operation": operation,
                "run_id": str(run_id or ""),
                "nonce": nonce,
                "name": name,
                "image": image,
                "context": context,
                "docker_cli": docker,
                "docker_cli_sha256": _bounded_file_digest(
                    docker, max_bytes=_DOCKER_CLI_MAX_BYTES
                ),
                "endpoint_sha256": str(endpoint_digest).lower(),
                "created_at": created,
                "expires_at": expires,
                "state": "armed",
                "process_stopped": False,
                "sequence": 1,
                "cleaned_at": None,
            }
            record = dict(unsigned)
            record["mac"] = self._mac(unsigned)
            _write_json(path, record)
        return OrphanLeaseHandle(
            path=path,
            nonce=nonce,
            operation=operation,
            endpoint_digest=str(endpoint_digest).lower(),
        )

    def mark_cleanup_pending(
        self,
        lease: OrphanLeaseHandle,
        *,
        now: int | None = None,
        process_stopped: bool = False,
    ) -> bool:
        try:
            if type(lease) is not OrphanLeaseHandle:
                return False
            if not isinstance(process_stopped, bool):
                return False
            directory = self._ensure_directory(create=False)
            if directory is None or lease.path.parent != directory:
                return False
            with _authorization_file_lock(self.lock_path):
                payload = self._read_record(lease.path)
                if (
                    payload["nonce"] != lease.nonce
                    or payload["operation"] != lease.operation
                    or payload["endpoint_sha256"] != lease.endpoint_digest
                ):
                    return False
                if payload["state"] == "cleaned":
                    return True
                if payload["state"] not in {"armed", "cleanup_pending"}:
                    return False
                if payload["process_stopped"] is True and process_stopped is False:
                    # Do not let a cleanup failure's best-effort ``finally``
                    # path erase a previously durable stop observation.
                    return False
                if now is not None and (
                    not isinstance(now, int) or isinstance(now, bool)
                ):
                    return False
                current = int(time.time()) if now is None else now
                if (
                    not self._valid_int(current)
                    or current < int(payload["created_at"])
                ):
                    return False
                self._write_transition(
                    lease.path,
                    payload,
                    state="cleanup_pending",
                    now=current,
                    process_stopped=process_stopped,
                )
            return True
        except Exception:
            return False

    def complete(self, lease: OrphanLeaseHandle, *, now: int | None = None) -> bool:
        """Seal a verified cleanup while retaining a bounded tombstone."""

        try:
            if type(lease) is not OrphanLeaseHandle:
                return False
            directory = self._ensure_directory(create=False)
            if directory is None or lease.path.parent != directory:
                return False
            with _authorization_file_lock(self.lock_path):
                payload = self._read_record(lease.path)
                if (
                    payload["nonce"] != lease.nonce
                    or payload["operation"] != lease.operation
                    or payload["endpoint_sha256"] != lease.endpoint_digest
                ):
                    return False
                if payload["state"] == "cleaned":
                    return True
                # Completion is a two-step capability: the authenticated
                # nonce/handle must first enter cleanup_pending, then the
                # process-stopped fact must be true.  Never permit an armed
                # record to jump directly to a cleaned tombstone.
                if payload["state"] != "cleanup_pending":
                    return False
                if payload["process_stopped"] is not True:
                    return False
                if now is not None and (
                    not isinstance(now, int) or isinstance(now, bool)
                ):
                    return False
                current = int(time.time()) if now is None else now
                if (
                    not self._valid_int(current)
                    or current < int(payload["created_at"])
                ):
                    return False
                self._write_transition(lease.path, payload, state="cleaned", now=current)
            return True
        except Exception:
            return False

    def _reap_record(self, payload: Mapping[str, Any], *, docker_cli: str = "") -> bool:
        """Run the already-authenticated, exact-owner reaper for one record."""

        stored_cli = _validate_docker_cli(payload.get("docker_cli"))
        if not stored_cli:
            return False
        selected_cli = _validate_docker_cli(docker_cli) if docker_cli else ""
        if docker_cli and not selected_cli:
            return False
        if selected_cli and selected_cli != stored_cli:
            return False
        try:
            observed_digest = _bounded_file_digest(
                stored_cli, max_bytes=_DOCKER_CLI_MAX_BYTES
            )
        except (OSError, ValueError):
            return False
        if observed_digest != payload.get("docker_cli_sha256"):
            return False
        endpoint = _local_docker_context_endpoint(stored_cli, payload["context"])
        if not endpoint or _digest(endpoint) != payload["endpoint_sha256"]:
            return False
        try:
            return bool(
                _reap_owned_eval_container(
                    stored_cli,
                    payload["context"],
                    payload["nonce"],
                    payload["image"],
                    name_prefix="brain-memory-p7-" + payload["operation"] + "-",
                    expected_endpoint_digest=payload["endpoint_sha256"],
                    expected_docker_cli_digest=payload["docker_cli_sha256"],
                )
            )
        except Exception:
            return False

    def sweep(
        self,
        *,
        docker_cli: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Reap only authenticated, expired leases; never broaden a target."""

        empty = {
            "ready": True,
            "pending_count": 0,
            "reaped_count": 0,
            "deferred_count": 0,
            "cleaned_count": 0,
            "reason": "",
        }
        try:
            if not self.persistent:
                return {
                    **empty,
                    "ready": False,
                    "reason": "orphan_lease_key_unavailable",
                }
            directory = self._ensure_directory(create=False)
            if directory is None:
                return empty
            if now is not None and (
                not isinstance(now, int) or isinstance(now, bool)
            ):
                return {**empty, "ready": False, "reason": "orphan_lease_invalid"}
            current = int(time.time()) if now is None else now
            if not self._valid_int(current):
                return {**empty, "ready": False, "reason": "orphan_lease_invalid"}
            if docker_cli is not None and not _validate_docker_cli(docker_cli):
                return {
                    **empty,
                    "ready": False,
                    "reason": "orphan_cleanup_unverified",
                }
            with _authorization_file_lock(self.lock_path):
                records = self._records(directory)
                try:
                    recovery_ledger = self._load_recovery_ledger(records, verify_epoch=True)
                except ValueError as exc:
                    reason = str(exc)
                    if reason not in {
                        "orphan_recovery_host_epoch_unavailable",
                        "orphan_recovery_host_mismatch",
                    }:
                        reason = "orphan_recovery_registry_invalid"
                    return {
                        **empty,
                        "ready": False,
                        "reason": reason,
                    }
                except Exception:
                    return {
                        **empty,
                        "ready": False,
                        "reason": "orphan_recovery_registry_invalid",
                    }
                recovered: set[str] = set()
                if recovery_ledger is not None:
                    if recovery_ledger["pending_index"] is not None:
                        pending = recovery_ledger["entries"][recovery_ledger["pending_index"]]
                        current_snapshot = {
                            payload["nonce"]: _digest(_canonical(payload))
                            for _path, payload in records
                        }
                        if pending["checkpoint"]["records"] != current_snapshot:
                            return {
                                **empty,
                                "ready": False,
                                "reason": "orphan_recovery_registry_changed",
                            }
                        return {
                            **empty,
                            "ready": False,
                            "reason": "orphan_recovery_checkpoint_pending",
                        }
                    for entry in recovery_ledger["entries"]:
                        if entry["receipt"] is None:
                            continue
                        for nonce in entry["checkpoint"]["targets"]:
                            payload = next(
                                (item for _path, item in records if item["nonce"] == nonce),
                                None,
                            )
                            if payload is None or not _reap_owned_eval_container(
                                payload["docker_cli"], payload["context"], payload["nonce"], payload["image"],
                                name_prefix="brain-memory-p7-" + payload["operation"] + "-",
                                expected_endpoint_digest=payload["endpoint_sha256"],
                                expected_docker_cli_digest=payload["docker_cli_sha256"],
                                allow_removal=False,
                            ):
                                return {
                                    **empty,
                                    "ready": False,
                                    "reason": "orphan_recovery_resources_unverified",
                                }
                            recovered.add(nonce)
                # Validate every record before issuing any Docker command.  A
                # malformed sibling must not be hidden by a successful one.
                pending: list[tuple[Path, dict[str, Any]]] = []
                expired: list[tuple[Path, dict[str, Any]]] = []
                tombstones: list[tuple[Path, dict[str, Any]]] = []
                deferred = 0
                for path, payload in records:
                    if payload["state"] == "cleaned":
                        tombstones.append((path, payload))
                    elif payload["nonce"] in recovered:
                        continue
                    elif current <= int(payload["expires_at"]) + _ORPHAN_LEASE_GRACE_SEC:
                        pending.append((path, payload))
                        deferred += 1
                    else:
                        # An expired lease alone does not prove that the
                        # creator/wrapper process tree stopped.  Only a
                        # bounded result which was durably marked stopped may
                        # enter the nonce reaper; a host hard-kill remains a
                        # manual/safe-stop condition.
                        if payload["process_stopped"] is not True:
                            return {
                                **empty,
                                "ready": False,
                                "pending_count": 1,
                                "deferred_count": deferred,
                                "cleaned_count": 0,
                                "reason": "orphan_cleanup_unverified",
                            }
                        expired.append((path, payload))
                if pending:
                    return {
                        **empty,
                        "ready": False,
                        "pending_count": len(pending),
                        "deferred_count": deferred,
                        "cleaned_count": 0,
                        "reason": "orphan_lease_live",
                    }
                # A killed CLI request can be materialized by the daemon after
                # an earlier empty observation.  Retain every authenticated
                # nonce tombstone and re-query it before each future operation;
                # finite time alone never proves that the race is gone forever.
                cleaned = 0
                for _path, payload in tombstones:
                    if not self._reap_record(
                        payload, docker_cli=docker_cli or ""
                    ):
                        return {
                            **empty,
                            "ready": False,
                            "pending_count": 1,
                            "deferred_count": deferred,
                            "cleaned_count": cleaned,
                            "reason": "orphan_cleanup_unverified",
                        }
                    cleaned += 1
                reaped = 0
                for path, payload in expired:
                    owned = self._reap_record(payload, docker_cli=docker_cli or "")
                    if not owned:
                        try:
                            self._write_transition(
                                path, payload, state="cleanup_pending", now=current
                            )
                        except (OSError, ValueError, RuntimeError):
                            pass
                        return {
                            **empty,
                            "ready": False,
                            "pending_count": len(expired) - reaped,
                            "deferred_count": deferred,
                            "cleaned_count": cleaned,
                            "reason": "orphan_cleanup_unverified",
                        }
                    try:
                        self._write_transition(
                            path,
                            payload,
                            state="cleaned",
                            now=current,
                            process_stopped=True,
                        )
                    except (OSError, ValueError, RuntimeError):
                        return {
                            **empty,
                            "ready": False,
                            "pending_count": len(expired) - reaped,
                            "deferred_count": deferred,
                            "cleaned_count": cleaned,
                            "reason": "orphan_cleanup_unverified",
                        }
                    reaped += 1
                return {
                    **empty,
                    "reaped_count": reaped,
                    "deferred_count": deferred,
                    "cleaned_count": cleaned,
                }
        except Exception:
            return {
                **empty,
                "ready": False,
                "reason": "orphan_lease_invalid",
            }


@dataclass(frozen=True, slots=True)
class RunLayout:
    run_id: str
    root: Path
    active: Path
    candidate: Path
    fixtures: Path
    state_db: Path
    promotion_ledger: Path
    metadata: Path
    private_metadata: Path
    # These two sidecars live beside the run directory.  The run tree can be
    # exchanged during promotion, but its authorization manifest/lock must not
    # become part of the candidate's writable surface.
    manifest: Path
    authorization_lock: Path

    @classmethod
    def create(cls, run_root: Path) -> "RunLayout":
        base = Path(run_root).expanduser().resolve(strict=True)
        if _is_link_like(base) or not base.is_dir():
            raise ValueError("P7 run root must be a real directory")
        run_id = secrets.token_hex(8)
        root = Path(
            tempfile.mkdtemp(prefix=RUN_DIRECTORY_PREFIX + run_id + "-", dir=str(base))
        )
        return cls(
            run_id=run_id,
            root=root,
            active=root / "active",
            candidate=root / "candidate",
            fixtures=root / "fixtures",
            state_db=root / "state.sqlite",
            promotion_ledger=root / "promotion.jsonl",
            metadata=root / "run.json",
            private_metadata=root / "run.private.json",
            manifest=base
            / ("." + RUN_DIRECTORY_PREFIX + run_id + ".manifest"),
            authorization_lock=base
            / ("." + RUN_DIRECTORY_PREFIX + run_id + ".authorize"),
        )

    @classmethod
    def load(cls, run_id: str, run_root: Path) -> "RunLayout":
        if not re.fullmatch(r"[0-9a-f]{16}", str(run_id or "")):
            raise ValueError("invalid P7 run id")
        base = Path(run_root).expanduser().resolve(strict=True)
        matches = [
            path
            for path in base.iterdir()
            if path.is_dir() and path.name.startswith(RUN_DIRECTORY_PREFIX + run_id + "-")
        ]
        if len(matches) != 1:
            raise FileNotFoundError("P7 run was not found")
        root = matches[0]
        if _is_link_like(root) or not root.is_dir():
            raise ValueError("P7 run root is not a private directory")
        expected_dirs = ("active", "candidate", "fixtures")
        for name in expected_dirs:
            child = root / name
            if _is_link_like(child) or not child.is_dir():
                raise ValueError("P7 run tree has an unsafe directory")
        _assert_run_metadata_paths(root)
        return cls(
            run_id=str(run_id),
            root=root,
            active=root / "active",
            candidate=root / "candidate",
            fixtures=root / "fixtures",
            state_db=root / "state.sqlite",
            promotion_ledger=root / "promotion.jsonl",
            metadata=root / "run.json",
            private_metadata=root / "run.private.json",
            manifest=base
            / ("." + RUN_DIRECTORY_PREFIX + str(run_id) + ".manifest"),
            authorization_lock=base
            / ("." + RUN_DIRECTORY_PREFIX + str(run_id) + ".authorize"),
        )


@dataclass
class _Runtime:
    layout: RunLayout
    store: Any
    stem: Any
    harness: Any
    controller: Any
    source_attestor: Any
    sandbox_attestor: Any
    activation_attestor: Any

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.controller.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        """Stop a live stem before closing its durable collaborators."""

        try:
            task = getattr(self.stem, "_task", None)
            if task is not None and not task.done():
                await self.stem.stop()
        except Exception:
            # The caller is already handling the primary failure.  Closing
            # the store/controller remains best effort and never masks it.
            pass
        finally:
            self.close()


@dataclass(frozen=True, slots=True)
class HostBinding:
    authorized: bool = False


class P7ControlledHost:
    """The explicit external host boundary for one P7 run."""

    def __init__(
        self,
        repo_root: Path | str | None = None,
        *,
        run_root: Path | str | None = None,
        llm_client: Any = None,
        manifest_key: bytes | bytearray | str | None = None,
        target_id: str = DEFAULT_TARGET_ID,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).expanduser().resolve(strict=True)
        self.run_root = Path(run_root or tempfile.gettempdir()).expanduser().resolve(strict=True)
        self.llm_client = llm_client
        self.target = resolve_iteration_target(target_id)
        configured_key = (
            manifest_key
            if manifest_key is not None
            else os.environ.get(MANIFEST_KEY_ENV)
        )
        # A transient key keeps library-only/unit-test use safe, but a fresh
        # process cannot authorize such a run.  Production two-phase CLI use
        # must provide the same external environment key to both invocations.
        self._manifest_key_persistent = configured_key is not None
        self._manifest_master_key = (
            _coerce_manifest_key(configured_key)
            if configured_key is not None
            else secrets.token_bytes(32)
        )

    def _target_summary(self) -> dict[str, Any]:
        return self.target.public_summary()

    def _target_binding_matches(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return (
            value.get("target_id") == self.target.target_id
            and value.get("scope") == self.target.scope
            and value.get("contract_digest") == target_contract_digest(self.target)
        )

    def _orphan_lease_store(self) -> OrphanLeaseStore:
        """Return the host-level Docker lease registry for this run root."""

        return OrphanLeaseStore(
            self.run_root,
            self._manifest_master_key,
            namespace=self.repo_root,
            persistent=self._manifest_key_persistent,
        )

    @staticmethod
    def _orphan_sweep_public(status: Mapping[str, Any]) -> dict[str, Any]:
        """Keep lease recovery status aggregate-only at the public boundary."""

        return {
            "ready": bool(status.get("ready")),
            "pending_count": int(status.get("pending_count", 0) or 0),
            "reaped_count": int(status.get("reaped_count", 0) or 0),
            "deferred_count": int(status.get("deferred_count", 0) or 0),
            "cleaned_count": int(status.get("cleaned_count", 0) or 0),
            "reason": _safe_text(status.get("reason"), 80),
        }

    def _run_secret(self, layout: RunLayout, purpose: str) -> bytes:
        """Derive stable, domain-separated host secrets without persisting them."""

        label = _safe_text(purpose, 80).lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", label):
            raise ValueError("P7 host secret purpose is invalid")
        material = f"brain-memory-p7-{label}-v1:{layout.run_id}".encode("utf-8")
        return hmac.new(self._manifest_master_key, material, hashlib.sha256).digest()

    def _write_fixture(
        self,
        layout: RunLayout,
        *,
        image_ref: str | None = None,
        context_name: str | None = None,
        docker_cli: str | None = None,
    ) -> None:
        image = _configured_docker_image(image_ref)
        docker = _configured_docker_cli(docker_cli)
        if not docker:
            raise ValueError("trusted Docker CLI is unavailable")
        context = _safe_text(
            context_name
            if context_name is not None
            else _configured_docker_context(docker),
            81,
        )
        layout.fixtures.mkdir(parents=True, exist_ok=False)
        wrapper = '''"""Fixture-owned Docker launcher; no candidate input controls its policy."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time

SECURITY_FLAGS = __SECURITY_FLAGS__
TMPFS_FLAG = __TMPFS_FLAG__
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,220}@sha256:[0-9a-f]{64}$")
CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
FULL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9a-f]{32,128}$")
MAX_OUTPUT = 65536
ALLOWED_ENV = {
    "PATH", "Path", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT",
    "USERPROFILE", "APPDATA", "DOCKER_CONFIG",
}
REQUEST_KEYS = {
    "schema_version", "protocol", "nonce", "candidate_fingerprint",
    "baseline_fingerprint", "fixture_hash", "evaluator_hash",
    "sandbox_contract_digest", "container_config_digest",
    "image_id_digest", "endpoint_digest",
    "executor_config_sha256", "wrapper_sha256", "judge_sha256",
    "candidate_target_sha256",
    "resource_budget",
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()

def link_like(path):
    try:
        current = Path(path)
        for item in (current, *current.parents):
            if item.is_symlink() or (
                hasattr(item, "is_junction") and item.is_junction()
            ):
                return True
            attributes = getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0)
            if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                return True
        return False
    except OSError:
        return True

def digest_file(path, limit=262144):
    path = Path(path)
    descriptor = None
    try:
        if link_like(path) or not path.is_file():
            return ""
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY
            | int(getattr(os, "O_NOFOLLOW", 0))
            | int(getattr(os, "O_BINARY", 0)),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1 or info.st_size > limit:
            return ""
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (len(data) > limit or info.st_size != after.st_size
                or info.st_mtime_ns != after.st_mtime_ns
                or getattr(info, "st_ino", 0) != getattr(after, "st_ino", 0)):
            return ""
    except (OSError, ValueError):
        return ""
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return digest_bytes(bytes(data)) if len(data) <= limit else ""

def trusted_docker(value):
    rendered = str(value or "").strip()
    if not rendered or "\\x00" in rendered or "\\r" in rendered or "\\n" in rendered:
        return ""
    source = Path(rendered)
    if not source.is_absolute() or source.name.lower() not in {"docker", "docker.exe"}:
        return ""
    try:
        if link_like(source) or not source.is_file():
            return ""
        resolved = source.resolve(strict=True)
        if resolved != source:
            return ""
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
            return ""
        if os.name != "nt" and not os.access(source, os.X_OK):
            return ""
    except (OSError, ValueError):
        return ""
    return str(resolved)

def bounded_digest(path, limit=262144):
    """Hash a mounted input without following aliases or accepting drift."""
    return digest_file(path, limit)

def safe_env():
    return {key: value for key, value in os.environ.items() if key in ALLOWED_ENV}

def create_kill_job(process):
    """Attach a Windows child to a kill-on-close Job Object."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateJobObjectW
        create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = None
        try:
            handle = create(None, None)
            if not handle:
                return None
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not set_info(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return None
            if not assign(handle, wintypes.HANDLE(process._handle)):
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

def close_kill_job(job):
    if job:
        try:
            return bool(job[0].CloseHandle(job[1]))
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    return True

def resume_suspended_process(process):
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

def fail(reason, metadata=None):
    payload = {"status": "fail", "verified": False, "error": reason}
    if isinstance(metadata, dict):
        payload["metadata"] = metadata
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2

def terminate_tree(process, job=None):
    """Stop a command and descendants without relying on a shell."""
    boundary_closed = True
    if job:
        boundary_closed = close_kill_job(job)
        if not boundary_closed and os.name == "nt":
            try:
                taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
                if taskkill.is_file() and not link_like(taskkill):
                    taskkill_result = subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                        timeout=3, check=False,
                    )
                    if getattr(taskkill_result, "returncode", None) is None:
                        boundary_closed = False
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    elif os.name == "nt":
        boundary_closed = False
        try:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
            if taskkill.is_file() and not link_like(taskkill):
                taskkill_result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                    timeout=3, check=False,
                )
                if getattr(taskkill_result, "returncode", None) is None:
                    boundary_closed = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group is already gone; this is a successful terminal state.
            pass
        except (AttributeError, OSError):
            # Only an already-absent group is success.  EPERM/EACCES keeps the
            # wrapper result untrusted so the host cannot attest a live tree.
            boundary_closed = False
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except (AttributeError, OSError, ValueError):
        boundary_closed = False
    return boundary_closed

def bounded_run(argv, timeout, limit=MAX_OUTPUT):
    """Run a Docker command while retaining at most limit+1 output bytes."""
    try:
        process_options = {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL, "shell": False, "env": safe_env(),
            "close_fds": True,
        }
        if os.name == "nt":
            process_options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | 0x00000004
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(list(argv), **process_options)
    except (OSError, ValueError):
        return None, b"", True, False, False
    job = None
    reader = None

    def terminate_safely():
        nonlocal job
        current_job = job
        # A Job handle is single-use once termination has been attempted.
        job = None
        try:
            return bool(terminate_tree(process, job=current_job))
        except BaseException:
            return False

    def abort_setup():
        """Stop a partially-created command before returning unknown state."""

        terminate_safely()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_safely()
            try:
                process.wait(timeout=1)
            except BaseException:
                pass
        except BaseException:
            # Keep the cleanup attempt fail-closed without masking a caller
            # exception (the setup caller re-raises its original exception).
            terminate_safely()
        try:
            if process.stdout is not None:
                process.stdout.close()
        except BaseException:
            pass
        if reader is not None:
            try:
                reader.join(timeout=1)
            except BaseException:
                pass

    try:
        job = create_kill_job(process)
        if os.name == "nt" and not job:
            abort_setup()
            return None, b"", False, False, False
        if not resume_suspended_process(process):
            abort_setup()
            return None, b"", False, False, False
    except BaseException:
        # Setup failures must not leave a Docker CLI outside the fixture's
        # process boundary.
        abort_setup()
        raise
    captured = bytearray()
    total = [0]
    overflow_event = threading.Event()
    drain_error = [False]
    boundary_closed = True

    def drain():
        stream = process.stdout
        if stream is None:
            drain_error[0] = True
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                total[0] += len(chunk)
                remaining = limit + 1 - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if total[0] > limit:
                    overflow_event.set()
        except Exception:
            drain_error[0] = True
        except BaseException:
            # An interrupted reader cannot attest that the child pipe/tree
            # was fully drained.
            drain_error[0] = True
        finally:
            try:
                stream.close()
            except BaseException:
                drain_error[0] = True

    try:
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
    except BaseException:
        abort_setup()
        raise
    timed_out = False
    cleanup_error = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow_event.is_set():
                boundary_closed = terminate_safely() and boundary_closed
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                boundary_closed = terminate_safely() and boundary_closed
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # Preserve interrupts while closing the wrapper's process boundary
        # before control leaves the fixture.
        boundary_closed = terminate_safely() and boundary_closed
        raise
    finally:
        try:
            process_exited = process.poll() is not None
        except BaseException as exc:
            process_exited = False
            boundary_closed = False
            cleanup_error = cleanup_error or exc
        if process_exited:
            # Normal parent exit is not proof that descendants exited.
            boundary_closed = terminate_safely() and boundary_closed
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out = True
            boundary_closed = terminate_safely() and boundary_closed
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                boundary_closed = False
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
        except BaseException as exc:
            boundary_closed = False
            cleanup_error = cleanup_error or exc
            boundary_closed = terminate_safely() and boundary_closed
        try:
            reader.join(timeout=3)
        except BaseException as exc:
            drain_error[0] = True
            cleanup_error = cleanup_error or exc
        try:
            reader_alive = reader.is_alive()
        except BaseException as exc:
            reader_alive = True
            drain_error[0] = True
            cleanup_error = cleanup_error or exc
        if reader_alive:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except BaseException:
                drain_error[0] = True
            try:
                reader.join(timeout=1)
            except BaseException as exc:
                drain_error[0] = True
                cleanup_error = cleanup_error or exc
        if job:
            try:
                boundary_closed = close_kill_job(job) and boundary_closed
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
            job = None
    try:
        reader_finished = not reader.is_alive()
    except BaseException as exc:
        reader_finished = False
        drain_error[0] = True
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error
    return (
        process.returncode,
        bytes(captured),
        len(captured) > limit,
        timed_out,
        reader_finished
        and not drain_error[0]
        and boundary_closed
        and process.returncode is not None,
    )

def mount(source, destination):
    source = Path(source)
    if not source.is_absolute() or link_like(source) or not source.is_dir():
        raise ValueError("unsafe_mount")
    try:
        resolved = source.resolve(strict=True)
    except (OSError, ValueError):
        raise ValueError("unsafe_mount")
    if resolved != source:
        raise ValueError("unsafe_mount")
    rendered = str(resolved)
    if any(character in rendered for character in ("\\x00", "\\r", "\\n", ",")):
        raise ValueError("unsafe_mount")
    return f"--mount=type=bind,src={rendered},dst={destination},readonly"

def request_payload():
    raw = os.environ.get("P7_EXECUTION_REQUEST", "")
    if not raw:
        return {}
    if len(raw.encode("utf-8", errors="ignore")) > 8192:
        raise ValueError("execution_request_too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("execution_request_invalid")
    if not isinstance(value, dict) or set(value) - REQUEST_KEYS:
        raise ValueError("execution_request_invalid")
    if value.get("schema_version") != 1 or value.get("protocol") != "p7-docker-v1":
        raise ValueError("execution_request_invalid")
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32,128}", nonce):
        raise ValueError("execution_request_invalid")
    required = {
        "candidate_target_sha256",
        "executor_config_sha256",
        "wrapper_sha256",
        "judge_sha256",
        "endpoint_digest",
    }
    if not required.issubset(value):
        raise ValueError("execution_request_invalid")
    # A full evaluation envelope must bind the candidate/baseline and every
    # fixed evaluator/sandbox input.  The smaller envelope is reserved for
    # the pre-need baseline replay diagnostic.
    if "candidate_fingerprint" in value:
        full_required = {
            "candidate_fingerprint", "baseline_fingerprint", "fixture_hash",
            "evaluator_hash", "sandbox_contract_digest", "container_config_digest",
            "image_id_digest", "endpoint_digest", "resource_budget",
        }
        if not full_required.issubset(value):
            raise ValueError("execution_request_invalid")
    for key in ("candidate_fingerprint", "baseline_fingerprint", "fixture_hash", "evaluator_hash", "sandbox_contract_digest", "container_config_digest", "image_id_digest", "endpoint_digest", "executor_config_sha256", "wrapper_sha256", "judge_sha256", "candidate_target_sha256"):
        if key in value and (not isinstance(value[key], str) or not HEX_RE.fullmatch(value[key])):
            raise ValueError("execution_request_invalid")
    if "resource_budget" in value and not isinstance(value["resource_budget"], dict):
        raise ValueError("execution_request_invalid")
    return value

def inspect_contract(value, image, image_id, ownership_nonce, created_id, container_name, candidate_root, fixture_root):
    if not isinstance(value, dict):
        return False, ""
    config = value.get("Config")
    host = value.get("HostConfig")
    mounts = value.get("Mounts")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(mounts, list):
        return False, ""
    expected_sources = {"/candidate": Path(candidate_root), "/fixtures": Path(fixture_root)}
    try:
        expected_sources = {
            key: value.resolve(strict=True) for key, value in expected_sources.items()
        }
    except (OSError, ValueError):
        return False, ""
    binds = host.get("Binds")
    if binds is not None and (not isinstance(binds, list) or len(binds) != len(expected_sources)):
        return False, ""

    def bind_matches(spec, source, destination):
        if not isinstance(spec, str):
            return False
        suffixes = (":" + destination + ":ro", ":" + destination + ":ro,rprivate")
        suffix = next((item for item in suffixes if spec.endswith(item)), "")
        if not suffix:
            return False
        rendered = spec[:-len(suffix)]
        if re.fullmatch(r"[A-Za-z]:[\\/].+", rendered):
            expected_windows = str(source).replace("/", "\\\\").rstrip("\\\\").casefold().removeprefix("\\\\\\\\?\\\\")
            actual_windows = rendered.replace("/", "\\\\").rstrip("\\\\").casefold().removeprefix("\\\\\\\\?\\\\")
            return actual_windows == expected_windows and not link_like(Path(rendered))
        try:
            actual = Path(rendered).resolve(strict=True)
        except (OSError, ValueError):
            return False
        return actual == source and not link_like(Path(rendered))

    if isinstance(binds, list) and not all(
        any(bind_matches(item, source, destination) for item in binds)
        for destination, source in expected_sources.items()
    ):
        return False

    mount_map = {}
    allowed_destinations = set(expected_sources) | {"/tmp", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
    for item in mounts:
        if not isinstance(item, dict):
            return False, ""
        destination = item.get("Destination")
        if not isinstance(destination, str) or destination in mount_map or destination not in allowed_destinations:
            return False, ""
        mount_map[destination] = item
        if destination in expected_sources:
            if item.get("Type") != "bind" or item.get("RW") is not False or not isinstance(item.get("Source"), str) or not item.get("Source"):
                return False, ""
        elif destination == "/tmp":
            if item.get("Type") != "tmpfs" or item.get("RW") is not True or item.get("Source") not in ("", None):
                return False, ""
        elif item.get("Type") != "bind" or item.get("RW") is not False:
            return False, ""

    if set(expected_sources) - set(mount_map):
        return False, ""

    def tmpfs_options_exact(options):
        if not isinstance(options, str):
            return False
        values = {item.strip().lower() for item in options.split(",") if item.strip()}
        required = {"rw", "noexec", "nosuid", "nodev"}
        size = next((item for item in values if item.startswith("size=")), "")
        try:
            raw_size = size[5:]
            size_bytes = int(raw_size[:-1]) * 1024 * 1024 if raw_size.endswith("m") else int(raw_size)
        except (TypeError, ValueError):
            return False
        return required <= values and size and size_bytes == 32 * 1024 * 1024 and values <= required | {size, "mode=1777"}

    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, dict) or set(tmpfs) != {"/tmp"} or not tmpfs_options_exact(tmpfs.get("/tmp")):
        return False, ""
    for key in ("CapAdd", "Devices", "DeviceRequests", "VolumesFrom", "Links", "PortBindings"):
        if host.get(key) not in (None, [], {}):
            return False, ""
    if host.get("Privileged", False) is not False or host.get("AutoRemove", False) not in (False, None):
        return False, ""
    if host.get("RestartPolicy") not in (None, {}, {"Name": "", "MaximumRetryCount": 0}, {"Name": "no", "MaximumRetryCount": 0}):
        return False, ""
    if host.get("PidMode") not in (None, "") or host.get("UTSMode") not in (None, "") or host.get("UsernsMode") not in (None, "") or host.get("CgroupnsMode") not in (None, "", "private"):
        return False, ""
    candidate_mount = mount_map.get("/candidate", {})
    fixture_mount = mount_map.get("/fixtures", {})
    try:
        expected_candidate = str(Path(candidate_root).resolve(strict=True))
        expected_fixtures = str(Path(fixture_root).resolve(strict=True))
        candidate_source = str(Path(candidate_mount.get("Source", "")).resolve(strict=True))
        fixture_source = str(Path(fixture_mount.get("Source", "")).resolve(strict=True))
    except (OSError, ValueError):
        return False, ""
    mounts_bound = (
        candidate_source == expected_candidate
        and fixture_source == expected_fixtures
        and not link_like(Path(candidate_mount.get("Source", "")))
        and not link_like(Path(fixture_mount.get("Source", "")))
    )
    tmp_entry = tmpfs.get("/tmp", "")
    ulimits = {
        item.get("Name"): (item.get("Soft"), item.get("Hard"))
        for item in host.get("Ulimits", [])
        if isinstance(item, dict)
    }
    network = host.get("NetworkMode") == "none" and host.get("IpcMode") == "none"
    filesystem = (
        host.get("ReadonlyRootfs") is True
        and candidate_mount.get("Type") == "bind"
        and candidate_mount.get("RW") is False
        and mounts_bound
        and fixture_mount.get("Type") == "bind"
        and fixture_mount.get("RW") is False
        and tmpfs_options_exact(tmp_entry)
    )
    privilege = (
        config.get("User") == "65534:65534"
        and {str(item).upper() for item in host.get("CapDrop", [])} == {"ALL"}
        and {str(item).lower() for item in host.get("SecurityOpt", [])}
        in ({"no-new-privileges"}, {"no-new-privileges:true"})
    )
    resources = (
        host.get("PidsLimit") == 64
        and host.get("Memory") == 256 * 1024 * 1024
        and host.get("MemorySwap") == 256 * 1024 * 1024
        and host.get("NanoCpus") == 1000000000
        and isinstance(host.get("LogConfig"), dict)
        and host.get("LogConfig", {}).get("Type") == "none"
        and host.get("ShmSize") == 16 * 1024 * 1024
        and host.get("Init") is True
        and ulimits.get("nofile") == (256, 256)
        and ulimits.get("nproc") == (64, 64)
    )
    image_ok = (
        config.get("Image") in {image, image_id}
        and str(value.get("Image", "")).lower() == image_id
    )
    inspected_id = str(value.get("Id", "")).lower()
    inspected_name = str(value.get("Name", ""))
    identity = (
        bool(FULL_ID_RE.fullmatch(inspected_id))
        and inspected_name in {container_name, "/" + container_name}
        and inspected_id == created_id.lower()
    )
    labels = config.get("Labels")
    ownership = (
        identity
        and
        isinstance(labels, dict)
        and labels.get("brain-memory.p7.nonce") == ownership_nonce
        and config.get("Image") in {image, image_id}
    )
    contract = {"network": network, "filesystem": filesystem, "privilege": privilege, "resources": resources, "image": image_ok, "identity": identity, "ownership": ownership}
    return all(contract.values()), digest_bytes(canonical(contract).encode("utf-8"))

def main():
    if len(sys.argv) != 3:
        return fail("sandbox_arguments_invalid")
    candidate_input = Path(sys.argv[1])
    fixtures_input = Path(sys.argv[2])
    if link_like(candidate_input) or link_like(fixtures_input):
        return fail("sandbox_roots_invalid")
    try:
        candidate = candidate_input.resolve(strict=True)
        fixtures = fixtures_input.resolve(strict=True)
    except (OSError, ValueError):
        return fail("sandbox_roots_invalid")
    if (
        not candidate.is_dir() or not fixtures.is_dir()
        or Path(__file__).resolve().parent != fixtures
    ):
        return fail("sandbox_roots_invalid")
    config_path = fixtures / "executor.json"
    wrapper_path = fixtures / "docker_executor.py"
    judge_path = fixtures / "judge.py"
    if link_like(config_path) or not config_path.is_file() or config_path.stat().st_size > 4096:
        return fail("sandbox_config_invalid")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return fail("sandbox_config_invalid")
    if not isinstance(config, dict):
        return fail("sandbox_config_invalid")
    image = config.get("image")
    context = config.get("context")
    docker = trusted_docker(config.get("docker_cli"))
    docker_hash = config.get("docker_cli_sha256")
    wrapper_hash = config.get("wrapper_sha256")
    judge_hash = config.get("judge_sha256")
    if (
        config.get("schema_version") != 1
        or config.get("protocol") != "p7-docker-v1"
        or not isinstance(image, str) or not IMAGE_RE.fullmatch(image)
        or not isinstance(context, str) or not CONTEXT_RE.fullmatch(context)
        or not docker
        or not isinstance(docker_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", docker_hash)
        or not isinstance(wrapper_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", wrapper_hash)
        or not isinstance(judge_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", judge_hash)
        or digest_file(docker, limit=268435456) != docker_hash
        or digest_file(wrapper_path) != wrapper_hash
        or digest_file(judge_path) != judge_hash
    ):
        return fail("sandbox_config_invalid")
    try:
        request = request_payload()
    except ValueError as exc:
        return fail(str(exc))
    if request and (
        request.get("wrapper_sha256", wrapper_hash) != wrapper_hash
        or request.get("judge_sha256", judge_hash) != judge_hash
        or request.get("executor_config_sha256", digest_file(config_path)) != digest_file(config_path)
    ):
        return fail("execution_request_binding_changed")
    if not docker:
        return fail("docker_cli_unavailable")
    # Refuse contexts that resolve to a remote transport.
    context_code, context_out, context_overflow, context_timed_out, context_drained = bounded_run(
        [docker, "context", "inspect", "--format={{json .Endpoints.docker.Host}}", context], 3, 4096
    )
    if context_code != 0 or context_overflow or context_timed_out or not context_drained:
        return fail("docker_context_unavailable")
    try:
        endpoint = json.loads(context_out.decode("utf-8", errors="strict").strip())
    except (ValueError, UnicodeError):
        endpoint = ""
    if not isinstance(endpoint, str) or not endpoint.lower().startswith(("npipe://", "unix://")):
        return fail("docker_context_not_local")
    endpoint_digest = digest_bytes(endpoint.encode("utf-8"))
    if request.get("endpoint_digest") != endpoint_digest:
        return fail("docker_endpoint_binding_changed")
    image_code, image_out, image_overflow, image_timed_out, image_drained = bounded_run(
        [docker, "--context", context, "image", "inspect", "--format={{.Id}}|{{json .RepoDigests}}", image], 5, 8192
    )
    if image_code != 0 or image_overflow or image_timed_out or not image_drained:
        return fail("docker_image_unavailable")
    try:
        image_id, repo_text = image_out.decode("utf-8", errors="strict").strip().split("|", 1)
        repo_digests = json.loads(repo_text)
    except (ValueError, UnicodeError):
        return fail("docker_image_unavailable")
    requested = image.rsplit("@sha256:", 1)[1]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id.strip().lower()) or not isinstance(repo_digests, list) or not any(isinstance(item, str) and item.lower().endswith("@sha256:" + requested) for item in repo_digests):
        return fail("docker_image_digest_mismatch")
    container_id = ""
    ownership_nonce = request.get("nonce") or secrets.token_hex(16)
    container_name = "brain-memory-p7-eval-" + ownership_nonce[:32]
    result_code = None
    result_out = b""
    result_overflow = False
    result_timed_out = False
    result_drained = False
    created_result = None
    inspect_result = None
    run_result = None
    owner_result = None
    clean_result = None
    post_result = None
    post2_result = None
    external_uncertain = False
    config_digest = ""
    inspect_verified = False
    cleanup_verified = False
    container_owned = False
    failure = ""

    def command_closed(result):
        # A timeout or output overflow is not a clean command result.  A
        # drained terminal state remains available to the durable orphan
        # lease, but it cannot authorize same-run destructive cleanup.
        return result is None or (
            result[0] is not None
            and not result[2]
            and not result[3]
            and result[4]
        )

    def cleanup_boundary_proven():
        return not external_uncertain and all(
            command_closed(item)
            for item in (
                created_result, inspect_result, run_result, owner_result,
                clean_result, post_result, post2_result,
            )
        )

    def docker_cli_matches():
        try:
            return digest_file(docker, limit=268435456) == docker_hash
        except (OSError, ValueError):
            return False

    def endpoint_matches(expected_digest):
        if not docker_cli_matches():
            return False
        state = bounded_run(
            [docker, "context", "inspect", "--format={{json .Endpoints.docker.Host}}", context],
            3,
            4096,
        )
        code, output, overflow, timed_out, drained = state
        if code != 0 or overflow or timed_out or not drained:
            return False
        try:
            current = json.loads(output.decode("utf-8", errors="strict").strip())
        except (ValueError, UnicodeError):
            return False
        return bool(
            isinstance(current, str)
            and current.lower().startswith(("npipe://", "unix://"))
            and digest_bytes(current.encode("utf-8")) == expected_digest
        )

    try:
        create_command = [
            docker, "--context", context, "create", *SECURITY_FLAGS,
            "--name=" + container_name,
            "--label=brain-memory.p7.nonce=" + ownership_nonce,
            "--workdir=/candidate", TMPFS_FLAG,
            mount(candidate, "/candidate"), mount(fixtures, "/fixtures"),
            "--env=PYTHONNOUSERSITE=1", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTHONUTF8=1", "--env=BRAIN_MEMORY_OFFLINE=1",
        ]
        if request:
            create_command.append("--env=P7_EXECUTION_REQUEST=" + canonical(request))
        create_command += ["--entrypoint=python", image, "/fixtures/judge.py", "/candidate"]
        created_result = bounded_run(create_command, 10, 4096)
        created_code, created_out, created_overflow, created_timed_out, created_drained = created_result
        created_id = created_out.decode("ascii", errors="ignore").strip()
        if created_code == 0 and not created_overflow and not created_timed_out and created_drained and FULL_ID_RE.fullmatch(created_id):
            container_id = created_id
        else:
            failure = "sandbox_create_failed"
        if not failure:
            inspect_result = bounded_run(
                [docker, "--context", context, "container", "inspect", "--format={{json .}}", container_id], 5, 262144
            )
            inspect_code, inspect_out, inspect_overflow, inspect_timed_out, inspect_drained = inspect_result
            if inspect_code != 0 or inspect_overflow or inspect_timed_out or not inspect_drained:
                failure = "sandbox_config_unverified"
            else:
                try:
                    inspected = json.loads(inspect_out.decode("utf-8", errors="strict"))
                except (ValueError, UnicodeError):
                    inspected = None
                inspect_verified, config_digest = inspect_contract(
                    inspected,
                    image,
                    image_id.strip().lower(),
                    ownership_nonce,
                    created_id,
                    container_name,
                    candidate,
                    fixtures,
                )
                # The create output is only an identifier.  It becomes a
                # cleanup target after the inspected nonce proves ownership.
                container_owned = bool(inspect_verified)
                if container_owned:
                    container_id = str(inspected.get("Id", "")).lower()
                if not inspect_verified:
                    failure = "sandbox_config_rejected"
                elif request and request.get("container_config_digest") not in (None, config_digest):
                    failure = "sandbox_config_binding_changed"
        if not failure:
            run_result = bounded_run(
                [docker, "--context", context, "start", "--attach", container_id], 25, MAX_OUTPUT
            )
            result_code, result_out, result_overflow, result_timed_out, result_drained = run_result
            if result_timed_out or not result_drained:
                failure = "sandbox_executor_timeout"
    except Exception:
        external_uncertain = True
        failure = failure or "sandbox_executor_failed"
    except BaseException:
        external_uncertain = True
        raise
    finally:
        cleanup_reference = container_id if container_owned else ""
        if not cleanup_reference:
            ownership_target = container_id or container_name
            try:
                owner_result = bounded_run(
                    [docker, "--context", context, "container", "inspect", "--format={{.Id}}|{{.Name}}|{{.Config.Image}}|{{index .Config.Labels \\\"brain-memory.p7.nonce\\\"}}", ownership_target], 3, 4096
                )
                owner_code, owner_out, owner_overflow, owner_timed_out, owner_drained = owner_result
                if owner_code == 0 and not owner_overflow and not owner_timed_out and owner_drained:
                    owner_fields = owner_out.decode("utf-8", errors="strict").strip().split("|", 3)
                    owner_id, owner_name, owner_image, owner_nonce = (
                        owner_fields if len(owner_fields) == 4 else ("", "", "", "")
                    )
                    if (
                        FULL_ID_RE.fullmatch(owner_id.lower())
                        and owner_name in {container_name, "/" + container_name}
                        and owner_image in {image, image_id.strip().lower()}
                        and owner_nonce == ownership_nonce
                    ):
                        # A name can be reused between inspection and removal;
                        # only the immutable ID is a safe cleanup target.
                        cleanup_reference = owner_id.lower()
                elif owner_code not in (None, 0) and not owner_overflow and not owner_timed_out and owner_drained:
                    # A generic inspect failure is not proof that the
                    # container is absent.  Keep cleanup unverified.
                    cleanup_verified = False
            except Exception:
                external_uncertain = True
                cleanup_verified = False
        if (
            FULL_ID_RE.fullmatch(cleanup_reference)
            and cleanup_boundary_proven()
            and docker_cli_matches()
            # The endpoint query is deliberately last before rm.
            and endpoint_matches(endpoint_digest)
        ):
            try:
                clean_result = bounded_run(
                    [docker, "--context", context, "container", "rm", "--force", cleanup_reference], 5, 4096
                )
                clean_code, _, clean_overflow, clean_timed_out, clean_drained = clean_result
                # Observe the ownership nonce after removal, rather than
                # querying only the old ID.  A delayed create can receive a
                # different ID while retaining this invocation's nonce.
                selector = "label=brain-memory.p7.nonce=" + ownership_nonce
                post_command = [
                    docker,
                    "--context",
                    context,
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    selector,
                    "--format={{.ID}}|{{.Names}}",
                ]
                post_result = bounded_run(
                    post_command, 3, 4096
                )
                post_code, post_out, post_overflow, post_timed_out, post_drained = post_result
                # A daemon can finish a delayed removal/create request just
                # after the first query.  Require two consecutive successful
                # empty observations before attesting cleanup.
                post2_result = bounded_run(
                    post_command, 3, 4096
                )
                post2_code, post2_out, post2_overflow, post2_timed_out, post2_drained = post2_result
                cleanup_verified = (
                    clean_code == 0
                    and not clean_overflow
                    and not clean_timed_out
                    and clean_drained
                    and post_code == 0
                    and not post_overflow
                    and not post_timed_out
                    and post_drained
                    and not post_out.strip()
                    and post2_code == 0
                    and not post2_overflow
                    and not post2_timed_out
                    and post2_drained
                    and not post2_out.strip()
                    and cleanup_boundary_proven()
                    and endpoint_matches(endpoint_digest)
                )
            except Exception:
                external_uncertain = True
                cleanup_verified = False
    if failure or not cleanup_verified or result_code != 0 or result_overflow or result_timed_out or not result_drained or not result_out or len(result_out) > MAX_OUTPUT:
        return fail(failure or ("sandbox_cleanup_failed" if not cleanup_verified else "sandbox_executor_failed"))
    if (
        digest_file(wrapper_path) != wrapper_hash
        or digest_file(judge_path) != judge_hash
        or digest_file(docker, limit=268435456) != docker_hash
    ):
        return fail("sandbox_fixture_changed_during_run")
    try:
        payload = json.loads(result_out.decode("utf-8", errors="strict").strip().splitlines()[-1])
    except (ValueError, UnicodeError, IndexError):
        return fail("sandbox_result_invalid")
    if not isinstance(payload, dict) or payload.get("verified") is not True:
        return fail("sandbox_result_unverified")
    input_integrity = payload.get("input_integrity")
    if request:
        if not isinstance(input_integrity, dict):
            return fail("sandbox_input_integrity_missing")
        expected_integrity = {
            "candidate_target_sha256_before": request.get("candidate_target_sha256"),
            "candidate_target_sha256_after": request.get("candidate_target_sha256"),
            "judge_sha256_before": request.get("judge_sha256"),
            "judge_sha256_after": request.get("judge_sha256"),
        }
        if input_integrity != expected_integrity:
            return fail("sandbox_input_integrity_changed")
    metadata = {
        "schema_version": 1,
        "protocol": "p7-docker-v1",
        "nonce": request.get("nonce", ""),
        "request_digest": digest_bytes(canonical(request).encode("utf-8")),
        "wrapper_sha256": wrapper_hash,
        "judge_sha256": judge_hash,
        "executor_config_sha256": digest_file(config_path),
        "container_config_digest": config_digest,
        "endpoint_digest": endpoint_digest,
        "inspect_verified": inspect_verified,
        "cleanup_verified": cleanup_verified,
        "image_id_digest": digest_bytes(image_id.strip().lower().encode("utf-8")),
        "input_integrity_verified": bool(request),
    }
    if request:
        metadata.update(input_integrity)
    if request.get("sandbox_contract_digest"):
        metadata["sandbox_contract_digest"] = request["sandbox_contract_digest"]
    if request.get("image_id_digest") and request["image_id_digest"] != metadata["image_id_digest"]:
        return fail("docker_image_binding_changed")
    payload["metadata"] = metadata
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''.replace("__SECURITY_FLAGS__", repr(_DOCKER_SECURITY_FLAGS)).replace(
            "__TMPFS_FLAG__", repr(_DOCKER_TMPFS_FLAG)
        )
        (layout.fixtures / "docker_executor.py").write_text(
            wrapper, encoding="utf-8", newline="\n"
        )
        judge = layout.fixtures / "judge.py"
        judge.write_text(
            '''"""Fixture-owned deterministic replay judge; candidate code cannot alter it."""\n
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import re
import threading
import time
import signal

SEEDS = (1, 5, 8, 13, 21, 34, 55, 89)
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
CODE = (
    "import json\\n"
    "from brain.drive_engine import GoalGenerator\\n"
    "activation = {'survival_drive': 0.0, 'curiosity_drive': 1.0, 'coherence_drive': 0.0, 'growth_drive': 0.0, 'exploration_drive': 0.0, 'creation_drive': 0.0, 'connection_drive': 0.0}\\n"
    "goals = GoalGenerator().generate(activation, ['identity'], ['Alpha', 'Beta'], [], 0, 17)\\n"
    "print(json.dumps([goal.description for goal in goals], ensure_ascii=False))\\n"
)

def link_like(path):
    """Reject symlink/junction/reparse aliases, including parent components."""
    try:
        current = Path(path)
        for item in (current, *current.parents):
            if item.is_symlink():
                return True
            attributes = getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0)
            if attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                return True
        return False
    except OSError:
        return True

def validate_tree(root, max_entries=20000, max_depth=64):
    """Reject links and multiply-linked files before importing candidate code."""
    pending = [(Path(root), 0)]
    seen = 0
    while pending:
        current, depth = pending.pop()
        if link_like(current):
            return False
        try:
            entries = list(os.scandir(current))
        except OSError:
            return False
        seen += len(entries)
        if seen > max_entries or depth > max_depth:
            return False
        for entry in entries:
            path = Path(entry.path)
            if link_like(path):
                return False
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            if entry.is_dir(follow_symlinks=False):
                pending.append((path, depth + 1))
            elif entry.is_file(follow_symlinks=False):
                if getattr(info, "st_nlink", 1) > 1:
                    return False
            else:
                return False
    return True

def digest_file(path, limit=262144):
    """Read one mounted file and reject aliases or in-read replacement."""
    path = Path(path)
    descriptor = None
    try:
        if link_like(path) or not path.is_file():
            return ""
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY
            | int(getattr(os, "O_NOFOLLOW", 0))
            | int(getattr(os, "O_BINARY", 0)),
        )
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode)
                or getattr(before, "st_nlink", 1) > 1
                or before.st_size > limit):
            return ""
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if (len(data) > limit
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)):
            return ""
    except (OSError, ValueError):
        return ""
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return hashlib.sha256(bytes(data)).hexdigest() if len(data) <= limit else ""

def request_hashes():
    raw = os.environ.get("P7_EXECUTION_REQUEST", "")
    if not raw or len(raw.encode("utf-8", errors="ignore")) > 8192:
        return "", ""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return "", ""
    if not isinstance(value, dict):
        return "", ""
    candidate_hash = value.get("candidate_target_sha256", "")
    judge_hash = value.get("judge_sha256", "")
    if (not isinstance(candidate_hash, str) or not HEX_RE.fullmatch(candidate_hash)
            or not isinstance(judge_hash, str) or not HEX_RE.fullmatch(judge_hash)):
        return "", ""
    return candidate_hash, judge_hash

def integrity_failure():
    print(json.dumps({"status": "fail", "verified": False,
                      "error": "mounted_input_integrity_failed"}, sort_keys=True))
    return 1

def create_kill_job(process):
    """Attach a Windows child to a kill-on-close Job Object."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateJobObjectW
        create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = None
        try:
            handle = create(None, None)
            if not handle:
                return None
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not set_info(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                return None
            if not assign(handle, wintypes.HANDLE(process._handle)):
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

def close_kill_job(job):
    if job:
        try:
            return bool(job[0].CloseHandle(job[1]))
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    return True

def resume_suspended_process(process):
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

def terminate_tree(process, job=None):
    """Stop the probe child and descendants without a shell."""
    boundary_closed = True
    if job:
        boundary_closed = close_kill_job(job)
        if not boundary_closed and os.name == "nt":
            try:
                taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
                if taskkill.is_file() and not link_like(taskkill):
                    taskkill_result = subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                        timeout=3, check=False,
                    )
                    if getattr(taskkill_result, "returncode", None) is None:
                        boundary_closed = False
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    elif os.name == "nt":
        boundary_closed = False
        try:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
            if taskkill.is_file() and not link_like(taskkill):
                taskkill_result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                    timeout=3, check=False,
                )
                if getattr(taskkill_result, "returncode", None) is None:
                    boundary_closed = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group is already gone; this is a successful terminal state.
            pass
        except (AttributeError, OSError):
            # Only an already-absent group is success.  EPERM/EACCES keeps the
            # wrapper result untrusted so the host cannot attest a live tree.
            boundary_closed = False
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except (AttributeError, OSError, ValueError):
        boundary_closed = False
    return boundary_closed

def bounded_child_run(argv, cwd, env, timeout, limit=65536):
    """Drain child output continuously while retaining only a bounded prefix."""
    try:
        process_options = {
            "cwd": str(cwd), "env": env, "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
            "shell": False, "close_fds": True,
        }
        if os.name == "nt":
            process_options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | 0x00000004
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(
            list(argv), **process_options,
        )
    except (OSError, ValueError):
        return None, b"", False, False, False
    job = None
    reader = None

    def terminate_safely():
        nonlocal job
        current_job = job
        job = None
        try:
            return bool(terminate_tree(process, job=current_job))
        except BaseException:
            return False

    def abort_setup():
        """Stop a partially-created probe child without masking its caller."""

        terminate_safely()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_safely()
            try:
                process.wait(timeout=1)
            except BaseException:
                pass
        except BaseException:
            terminate_safely()
        try:
            if process.stdout is not None:
                process.stdout.close()
        except BaseException:
            pass
        if reader is not None:
            try:
                reader.join(timeout=1)
            except BaseException:
                pass

    try:
        job = create_kill_job(process)
        if os.name == "nt" and not job:
            abort_setup()
            return None, b"", False, False, False
        if not resume_suspended_process(process):
            abort_setup()
            return None, b"", False, False, False
    except BaseException:
        # Setup failures must not leave the probe child outside its boundary.
        abort_setup()
        raise
    captured = bytearray()
    overflow = [False]
    overflow_event = threading.Event()
    drain_error = [False]
    boundary_closed = True

    def drain():
        stream = process.stdout
        if stream is None:
            drain_error[0] = True
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if len(captured) < limit + 1:
                    remaining = limit + 1 - len(captured)
                    captured.extend(chunk[:remaining])
                if len(captured) > limit:
                    overflow[0] = True
                    overflow_event.set()
        except Exception:
            drain_error[0] = True
        except BaseException:
            # A reader interrupted by an asynchronous exception is not a
            # trustworthy drained boundary.
            drain_error[0] = True
        finally:
            try:
                stream.close()
            except BaseException:
                drain_error[0] = True

    try:
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
    except BaseException:
        abort_setup()
        raise
    timed_out = False
    cleanup_error = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow_event.is_set():
                boundary_closed = terminate_safely() and boundary_closed
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                boundary_closed = terminate_safely() and boundary_closed
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # Preserve the exception, but never abandon a running Docker probe
        # child or its output pipe on the way out.
        boundary_closed = terminate_safely() and boundary_closed
        raise
    finally:
        # A normal parent exit does not prove that descendants released
        # stdout, so close the boundary before returning a proof.
        try:
            process_exited = process.poll() is not None
        except BaseException as exc:
            process_exited = False
            boundary_closed = False
            cleanup_error = cleanup_error or exc
        if process_exited:
            boundary_closed = terminate_safely() and boundary_closed
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out = True
            boundary_closed = terminate_safely() and boundary_closed
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                boundary_closed = False
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
        except BaseException as exc:
            boundary_closed = False
            cleanup_error = cleanup_error or exc
            boundary_closed = terminate_safely() and boundary_closed
        try:
            reader.join(timeout=3)
        except BaseException as exc:
            drain_error[0] = True
            cleanup_error = cleanup_error or exc
        try:
            reader_alive = reader.is_alive()
        except BaseException as exc:
            reader_alive = True
            drain_error[0] = True
            cleanup_error = cleanup_error or exc
        if reader_alive:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except BaseException:
                drain_error[0] = True
            try:
                reader.join(timeout=1)
            except BaseException as exc:
                drain_error[0] = True
                cleanup_error = cleanup_error or exc
        if job:
            try:
                boundary_closed = close_kill_job(job) and boundary_closed
            except BaseException as exc:
                boundary_closed = False
                cleanup_error = cleanup_error or exc
            job = None
    try:
        reader_finished = not reader.is_alive()
    except BaseException as exc:
        reader_finished = False
        drain_error[0] = True
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error
    return (
        process.returncode,
        bytes(captured),
        overflow[0],
        timed_out,
        reader_finished
        and not drain_error[0]
        and boundary_closed
        and process.returncode is not None,
    )

def child_failure(error):
    print(json.dumps({"status": "fail", "verified": False, "error": error}, sort_keys=True))
    return 1

def main() -> int:
    if len(sys.argv) != 2:
        return 2
    candidate_input = Path(sys.argv[1])
    if link_like(candidate_input):
        return 2
    try:
        candidate = candidate_input.resolve(strict=True)
    except (OSError, ValueError):
        return 2
    if not candidate.is_dir() or not validate_tree(candidate):
        return 2
    expected_candidate, expected_judge = request_hashes()
    target = candidate / "brain" / "drive_engine.py"
    target_before = digest_file(target)
    judge_before = digest_file(Path(__file__))
    if (not expected_candidate or not expected_judge
            or target_before != expected_candidate
            or judge_before != expected_judge):
        return integrity_failure()
    values = []
    observations = []
    for seed in SEEDS:
        env = {key: value for key, value in os.environ.items() if key in {'PATH', 'Path', 'SystemRoot', 'SYSTEMROOT', 'TEMP', 'TMP', 'PATHEXT'}}
        env.update({'PYTHONPATH': str(candidate), 'PYTHONHASHSEED': str(seed), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'BRAIN_MEMORY_OFFLINE': '1'})
        result_code, result_stdout, output_overflow, timed_out, drained = bounded_child_run(
            [sys.executable, '-c', CODE], candidate, env, 8
        )
        if timed_out:
            return child_failure('probe_child_timeout')
        if result_code is None:
            return child_failure('probe_child_spawn_failed')
        if output_overflow:
            return child_failure('probe_output_overflow')
        if not drained:
            return child_failure('probe_output_drain_failed')
        if result_code != 0:
            return child_failure('probe_child_failed')
        try:
            parsed = json.loads(result_stdout.decode('utf-8').strip().splitlines()[-1])
        except (ValueError, IndexError, UnicodeError):
            print(json.dumps({'status': 'fail', 'verified': False, 'error': 'probe_output_invalid'}))
            return 1
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            print(json.dumps({'status': 'fail', 'verified': False, 'error': 'probe_shape_invalid'}))
            return 1
        values.append(tuple(parsed))
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        observations.append({"seed": seed, "description_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "description_count": len(parsed)})
    target_after = digest_file(target)
    judge_after = digest_file(Path(__file__))
    input_integrity = {
        "candidate_target_sha256_before": target_before,
        "candidate_target_sha256_after": target_after,
        "judge_sha256_before": judge_before,
        "judge_sha256_after": judge_after,
    }
    if (target_after != expected_candidate or judge_after != expected_judge
            or target_after != target_before or judge_after != judge_before):
        return integrity_failure()
    unique = len(set(values))
    integrity = bool(values) and all(len(items) > 0 for items in values) and len({len(items) for items in values}) == 1
    print(json.dumps({'status': 'pass' if unique == 1 and integrity else 'fail', 'verified': True, 'metrics': {'deterministic_replay': 1.0 if unique == 1 else 0.0, 'unique_description_count': float(unique), 'goal_generation_integrity': 1.0 if integrity else 0.0}, 'primary_dimension': 'deterministic_replay', 'observations': observations, 'input_integrity': input_integrity}, sort_keys=True))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
''',
            encoding="utf-8",
            newline="\n",
        )
        # The executor policy names its immutable code explicitly.  The
        # wrapper rechecks these hashes before launching Docker, closing the
        # fixture TOCTOU gap without placing any path or source text in a
        # public receipt.
        _write_json(
            layout.fixtures / "executor.json",
            {
                "schema_version": HOST_SCHEMA_VERSION,
                "protocol": _SANDBOX_PROTOCOL,
                "image": image,
                "context": context,
                "docker_cli": docker,
                "docker_cli_sha256": _bounded_file_digest(
                    docker, max_bytes=_DOCKER_CLI_MAX_BYTES
                ),
                "wrapper_sha256": _bounded_file_digest(
                    layout.fixtures / "docker_executor.py"
                ),
                "judge_sha256": _bounded_file_digest(layout.fixtures / "judge.py"),
            },
        )

    def _build_runtime(
        self, layout: RunLayout, *, sandbox: Mapping[str, Any]
    ) -> _Runtime:
        from brain.brain_stem import BrainStem
        from brain.evaluation_harness import EvaluationHarness, ResourceBudget
        from brain.evolution import PromotionController, SandboxAttestor
        from brain.homeostasis import ControlledEnvironment
        from brain.motivation import MotivationSourceAttestor
        from brain.succession_runtime import SuccessorActivationAttestor
        from storage.database import StateStore, init_db

        _assert_run_metadata_paths(layout.root)
        init_db(str(layout.state_db))
        store = StateStore(str(layout.state_db))
        source_attestor = MotivationSourceAttestor(
            self._run_secret(layout, "motivation-source"),
            issuer_id="p7-host",
            replay_store=store,
        )
        sandbox_attestor = SandboxAttestor(
            secret=self._run_secret(layout, "sandbox-attestation"),
            issuer_id="p7-sandbox",
        )
        activation_attestor = SuccessorActivationAttestor(
            secret=self._run_secret(layout, "succession-activation"),
            issuer_id="p7-succession",
        )
        harness = EvaluationHarness(
            fixtures=layout.fixtures,
            command=[
                sys.executable,
                "-I",
                "{fixtures}/docker_executor.py",
                "{candidate}",
                "{fixtures}",
            ],
            resource_budget=ResourceBudget(timeout_sec=60),
            primary_dimension="deterministic_replay",
            critical_dimensions=(
                "deterministic_replay",
                "goal_generation_integrity",
            ),
        )
        controller = PromotionController(
            layout.active,
            harness=harness,
            profile="production",
            sandbox_attestor=sandbox_attestor,
            ledger_path=layout.promotion_ledger,
            persistence_path=layout.state_db,
            protected_files=("tools/p7_controlled_host.py",),
        )
        environment = ControlledEnvironment(
            kind="evaluation", root=layout.active, network_enabled=False
        )
        stem = BrainStem(
            state_store=store,
            motivation_source_attestor=source_attestor,
            controlled_environment=environment,
            controlled_host_status=sandbox,
            evaluation_harness=harness,
            promotion_controller=controller,
            succession_profile="production",
            succession_activation_attestor=activation_attestor,
        )
        # Bind the coordinator only after the stem has restored its durable
        # LifeKernel.  Constructing it before a restart would claim the
        # constructor's throw-away kernel object in the process-local guard.
        return _Runtime(
            layout,
            store,
            stem,
            harness,
            controller,
            source_attestor,
            sandbox_attestor,
            activation_attestor,
        )

    @staticmethod
    def _evaluator_contract(
        runtime: _Runtime, sandbox: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bind every evaluator input that can affect an authorization receipt."""

        return {
            "harness_version": runtime.harness.harness_version,
            "fixture_hash": runtime.harness.fixture_hash,
            "evaluator_hash": runtime.harness.evaluator_hash,
            "resource_budget": runtime.harness.resource_budget.to_dict(),
            "sandbox_contract_digest": sandbox.get("contract_digest"),
        }

    async def _start_and_stop(self, stem: Any) -> dict[str, Any]:
        await stem.start()
        # This existing BrainStem seam wires the three durable succession
        # sinks; it does not create or activate a successor.
        if getattr(stem, "succession_coordinator", None) is None:
            stem._get_succession_coordinator()
        snapshot = stem.continuity_readiness_snapshot()
        await stem.stop()
        return snapshot

    async def _generate_model_patch(
        self,
        evidence: Mapping[str, Any],
        *,
        source_root: Path | str | None = None,
    ) -> tuple[ModelPatch | None, str]:
        # An injected object can imitate the provider metadata while never
        # contacting DeepSeek.  Keep dependency injection out of the
        # authorizing path; tests should exercise validation/evaluation with
        # explicit fake helpers, never produce a promotable receipt.
        if self.llm_client is not None:
            return None, "injected_model_not_allowed"
        if not os.environ.get("DEEPSEEK_API_KEY"):
            return None, "deepseek_key_unavailable"
        from services.llm_client import LLMClient

        client = LLMClient()
        if type(client) is not LLMClient:
            return None, "model_client_untrusted"
        cfg = getattr(client, "cfg", {})
        if cfg and (
            str(cfg.get("provider", "")).lower() != "deepseek"
            or str(cfg.get("model", "")) != "deepseek-v4-flash"
            or str(cfg.get("base", "")).rstrip("/") != "https://api.deepseek.com"
        ):
            return None, "model_channel_not_deepseek"
        target_root = (
            Path(source_root).expanduser().resolve(strict=True)
            if source_root is not None
            else self.repo_root
        )
        target = target_root / self.target.scope
        if not target.is_file() or _is_link_like(target):
            return None, "model_source_unavailable"
        source = target.read_text(encoding="utf-8")
        # Keep the model context minimal and anchored to the measured line;
        # sending a broad file encourages a model to invent a nearby variable
        # or rewrite unrelated documentation.
        marker = "hash(str(current_tick) + drive_name)"
        marker_index = source.find(marker)
        if marker_index < 0:
            return None, "model_source_gap_changed"
        target_line_number = source[:marker_index].count("\n") + 1
        lines = source.splitlines()
        logging_line_numbers = [
            index + 1 for index, line in enumerate(lines) if line.strip() == "import logging"
        ]
        if len(logging_line_numbers) != 1:
            return None, "model_source_import_anchor_changed"
        import_line_number = logging_line_numbers[0]
        context_start = max(0, target_line_number - 18)
        context_end = min(len(lines), target_line_number + 12)
        excerpt = "\n".join(
            f"{index + 1}: {lines[index]}"
            for index in range(context_start, context_end)
        )
        system = (
            "你是受控代码评估宿主中的补丁生成器。只返回 JSON (a valid json object)，不要 markdown。"
            "只能修改 brain/drive_engine.py 中 GoalGenerator 的跨进程确定性问题；"
            "不得修改身份、生命周期、动机阈值、评估器、晋升、继承、网络或依赖。"
            "必须使用 Python 标准库 hashlib 替换内置 hash(str(current_tick)+drive_name)，"
            "并使用 int(hashlib.sha256((str(current_tick)+drive_name).encode(\"utf-8\")).hexdigest(), 16)"
            "作为 entities_pool 的索引种子，保持其余行为不变。只允许两处改动：在现有"
            "import logging 后增加 import hashlib，以及替换目标 entity 查找行。字段必须是 title、scope、"
            "hypothesis、unified_diff。返回形如 {\"title\":\"...\",\"scope\":\"brain/drive_engine.py\",\"hypothesis\":\"...\",\"unified_diff\":\"...\"} 的 json 对象。unified_diff 必须是可被 git apply --check 接受的完整补丁；"
            f"目标旧行在第 {target_line_number} 行，import logging 在第 {import_line_number} 行；"
            "每个 hunk 必须使用与这些真实行号匹配的 @@ -行号,数量 +行号,数量 @@ 标题，禁止裸 @@。"
        )
        user = _canonical(
            {
                "scope": self.target.scope,
                "required_old_expression": "entities_pool[hash(str(current_tick) + drive_name)",
                "required_import_context": "import logging",
                "target_line_number": target_line_number,
                "evidence": {
                    "static_digest": evidence.get("static", {}).get("evidence_digest"),
                    "runtime_digest": evidence.get("runtime", {}).get("observation_digest"),
                },
                "source_excerpt": excerpt,
            }
        )
        # A provider can return valid JSON with an inapplicable diff.  Retry a
        # small, fixed number of times while keeping every accepted
        # replacement genuinely model-generated.  Structural canonicalization
        # happens only after this method returns and never invents code.
        last_reason = "model_patch_rejected"
        for _attempt in range(3):
            try:
                response = await client.chat_json(
                    system=system, user=user, temperature=0.1, max_tokens=4096
                )
            except Exception as exc:
                last_reason = "deepseek_request_failed:" + type(exc).__name__
                continue
            patch = coerce_model_patch(response)
            if patch is not None:
                return patch, "ok"
            last_reason = "model_patch_rejected"
            system += " 上次响应未通过补丁门；这次必须提供带数字行号且可应用的 hunk。"
        return None, last_reason

    async def _generate_and_apply_candidate(
        self,
        evidence: Mapping[str, Any],
        *,
        active: Path,
        candidate: Path,
    ) -> tuple[ModelPatch | None, str]:
        """Obtain and apply one model patch, retrying only rejected diffs."""

        last_reason = "model_patch_rejected"
        for _attempt in range(3):
            patch, reason = await self._generate_model_patch(
                evidence, source_root=active
            )
            if patch is None:
                last_reason = reason
                continue
            try:
                canonical_patch = _canonical_model_patch(patch, active)
                _apply_unified_diff(candidate, canonical_patch, baseline=active)
                return canonical_patch, "ok"
            except (RuntimeError, OSError):
                last_reason = "model_patch_not_applicable"
                # A failed check should not normally mutate the tree, but a
                # defensive exact-tree rebuild prevents a partial Git apply
                # from contaminating the next model attempt.
                try:
                    if candidate.exists() and candidate.is_dir() and not _is_link_like(candidate):
                        shutil.rmtree(candidate)
                    _safe_copy_tree(active, candidate)
                except Exception:
                    return None, "candidate_reset_failed"
        return None, last_reason

    def _record_attested_need_with_binding(
        self,
        runtime: _Runtime,
        evidence: Mapping[str, Any],
        run_id: str,
    ) -> tuple[Any, dict[str, Any]]:
        """Turn four concrete observations into a signed, replayable need.

        The events are not free-standing threshold votes: two describe
        distinct static facts and two are disjoint runtime seed groups.  Their
        intensity/persistence are derived from the observed predicates, and
        the durable replay keys are retained as opaque host evidence for the
        second phase.
        """

        from brain.motivation import ImpulseEvent

        static = evidence.get("static")
        runtime_probe = evidence.get("runtime")
        observations = runtime_probe.get("observations") if isinstance(runtime_probe, Mapping) else None
        if not isinstance(static, Mapping) or not isinstance(observations, list) or len(observations) < 4:
            raise RuntimeError("attested evidence set is incomplete")
        execution_metadata = (
            runtime_probe.get("execution_metadata")
            if isinstance(runtime_probe, Mapping)
            else None
        )
        if (
            not isinstance(execution_metadata, Mapping)
            or execution_metadata.get("inspect_verified") is not True
            or execution_metadata.get("cleanup_verified") is not True
        ):
            raise RuntimeError("runtime evidence lacks a durable sandbox proof")
        execution_proof_digest = _digest(_canonical(dict(execution_metadata)))
        if not bool(static.get("builtin_hash_detected")) or not bool(static.get("goal_generator_call_detected")):
            raise RuntimeError("static evidence predicates are not established")
        if bool(runtime_probe.get("replayable")):
            raise RuntimeError("runtime evidence does not establish a replay gap")
        if runtime_probe.get("goal_generation_integrity") is not True:
            raise RuntimeError("runtime evidence does not preserve goal generation")

        # Cross-group comparisons make the repeated runtime signal a
        # replayable observation rather than an arbitrary duplicate counter.
        differing_pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        used_indices: set[int] = set()
        for left_index in range(len(observations)):
            for right_index in range(left_index + 1, len(observations)):
                left = observations[left_index]
                right = observations[right_index]
                if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                    continue
                if str(left.get("description_digest", "")) == str(
                    right.get("description_digest", "")
                ):
                    continue
                if left_index in used_indices or right_index in used_indices:
                    continue
                differing_pairs.append((left, right))
                used_indices.update((left_index, right_index))
                if len(differing_pairs) == 2:
                    break
            if len(differing_pairs) == 2:
                break
        if len(differing_pairs) != 2:
            raise RuntimeError("runtime evidence lacks two independent replay divergences")
        pair_a, pair_b = differing_pairs
        pair_digest_a = _digest(_canonical(pair_a))
        pair_digest_b = _digest(_canonical(pair_b))
        grouped = (
            ("static", "builtin_hash", static.get("target_digest", ""), True),
            ("static", "goal_generator_call", static.get("call_site_digest", ""), True),
            (
                "runtime",
                "seed_group_a",
                pair_digest_a,
                str(pair_a[0].get("description_digest", ""))
                != str(pair_a[1].get("description_digest", "")),
            ),
            (
                "runtime",
                "seed_group_b",
                pair_digest_b,
                str(pair_b[0].get("description_digest", ""))
                != str(pair_b[1].get("description_digest", "")),
            ),
        )
        now = time.time()
        proofs: list[dict[str, Any]] = []
        for index, (label, fact, digest, predicate) in enumerate(grouped):
            if not predicate or not re.fullmatch(r"[0-9a-f]{64}", str(digest).lower()):
                raise RuntimeError("evidence predicate lacks a stable digest")
            event = ImpulseEvent.create(
                impulse_type="growth",
                intensity=1.0,
                source="p7-" + label,
                source_kind="external",
                context=f"{fact}:{str(digest).lower()[:24]}",
                timestamp=now + index,
                event_id=f"p7-{run_id}-{fact}-{index}",
                persistence=1.0,
                unresolved=True,
                reliability=1.0,
                metadata={"evidence_kind": label, "evidence_fact": fact, "digest": str(digest).lower()},
            )
            attestation = runtime.source_attestor.issue_for_event(
                event, source_id="p7-source-" + label, now=now + index
            )
            event = replace(event, provenance=attestation)
            runtime.stem.record_impulse(event, now=now + index)
            proofs.append(
                {
                    "event_id": event.event_id,
                    "replay_key": attestation.replay_key,
                    "attestation_hash": attestation.attestation_hash,
                    "source_label": label,
                    "fact": fact,
                    "evidence_digest": str(digest).lower(),
                    "event_payload_hash": attestation.payload_hash,
                    "attestation": attestation.to_dict(include_signature=True),
                }
            )
        need = runtime.stem.poll_iteration_need("growth", now=now + len(grouped))
        if need is None:
            raise RuntimeError("attested evidence did not reach the configured motivation threshold")
        # Bind the complete request, including its normalized ISO timestamp;
        # ``IterationNeed`` now preserves that value across serialization.
        binding_need = self._need_payload(need)
        binding_payload = {
            "need": binding_need,
            "proofs": proofs,
        }
        binding = {
            "schema_version": HOST_SCHEMA_VERSION,
            "need_digest": _digest(_canonical(binding_need)),
            "proofs": proofs,
            "proof_count": len(proofs),
            "binding_digest": _digest(_canonical(binding_payload)),
            "execution_proof_digest": execution_proof_digest,
        }
        return need, binding

    def _record_attested_need(
        self, runtime: _Runtime, evidence: Mapping[str, Any], run_id: str
    ) -> Any:
        """Compatibility wrapper returning only the need object."""

        need, _binding = self._record_attested_need_with_binding(runtime, evidence, run_id)
        return need

    @staticmethod
    def _need_payload(need: Any) -> dict[str, Any]:
        return {
            "need_id": _safe_text(getattr(need, "need_id", ""), 100),
            "motive": _safe_text(getattr(need, "motive", ""), 80),
            "pressure": round(float(getattr(need, "pressure", 0.0)), 6),
            "trigger": _safe_text(getattr(need, "trigger", ""), 80),
            "reason": _safe_text(getattr(need, "reason", ""), 500),
            "evidence": [_safe_text(item, 300) for item in (getattr(need, "evidence", ()) or ())],
            "source_labels": [_safe_text(item, 120) for item in (getattr(need, "source_labels", ()) or ())],
            "urgency": round(float(getattr(need, "urgency", 0.0)), 6),
            "confidence": round(float(getattr(need, "confidence", 0.0)), 6),
            "created_at": _safe_text(getattr(need, "created_at", ""), 100),
        }

    @staticmethod
    def _need_summary(need: Any) -> dict[str, Any]:
        payload = P7ControlledHost._need_payload(need)
        payload.update(
            {
                "evidence_count": len(payload["evidence"]),
                "source_count": len(payload["source_labels"]),
                "requires_evaluation": True,
                "authorization": False,
            }
        )
        return payload

    @staticmethod
    def _receipt_summary(receipt: Any) -> dict[str, Any]:
        metadata = getattr(receipt, "metadata", {})
        metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
        return {
            "accepted": bool(receipt.accepted),
            "receipt_hash": _safe_text(receipt.receipt_hash, 128),
            "candidate_metrics": dict(receipt.candidate_metrics),
            "primary_dimension": _safe_text(receipt.primary_dimension, 120),
            "improvement": receipt.improvement,
            "hard_gates_passed": bool(receipt.hard_gates_passed),
            "failed_gates": list(receipt.failed_gates),
            "result_verified": bool(receipt.result_verified),
            "candidate_fingerprint": _safe_text(
                receipt.candidate_fingerprint, 128
            ).lower(),
            "baseline_fingerprint": _safe_text(
                receipt.baseline_fingerprint, 128
            ).lower(),
            "mode": _safe_text(receipt.mode, 40).lower(),
            # Keep the public receipt bounded while retaining a digest of the
            # executor proof for restarted semantic comparison.
            "execution_proof_digest": _digest(_canonical(metadata_map))
            if metadata_map
            else "",
            "execution_proof_verified": bool(
                metadata_map.get("inspect_verified") is True
                and metadata_map.get("cleanup_verified") is True
            ),
        }

    @staticmethod
    def _execution_request(
        runtime: _Runtime,
        sandbox: Mapping[str, Any],
        *,
        candidate_fingerprint: str,
        baseline_fingerprint: str,
        nonce: str | None = None,
        target_scope: str = TARGET_SCOPE,
    ) -> dict[str, Any]:
        """Build the non-secret envelope sent to the fixture Docker wrapper."""

        layout = runtime.layout
        wrapper_hash = _bounded_file_digest(layout.fixtures / "docker_executor.py")
        judge_hash = _bounded_file_digest(layout.fixtures / "judge.py")
        config_hash = _bounded_file_digest(layout.fixtures / "executor.json")
        request = {
            "schema_version": HOST_SCHEMA_VERSION,
            "protocol": _SANDBOX_PROTOCOL,
            "nonce": nonce or secrets.token_hex(16),
            "candidate_target_sha256": _bounded_file_digest(
                layout.candidate / target_scope
            ),
            "candidate_fingerprint": _safe_text(candidate_fingerprint, 128).lower(),
            "baseline_fingerprint": _safe_text(baseline_fingerprint, 128).lower(),
            "fixture_hash": runtime.harness.fixture_hash,
            "evaluator_hash": runtime.harness.evaluator_hash,
            "sandbox_contract_digest": _safe_text(
                sandbox.get("contract_digest"), 128
            ).lower(),
            "container_config_digest": _safe_text(
                sandbox.get("container_config_digest"), 128
            ).lower(),
            "image_id_digest": _safe_text(
                sandbox.get("image_id_digest"), 128
            ).lower(),
            "endpoint_digest": _safe_text(
                sandbox.get("endpoint_digest"), 128
            ).lower(),
            "executor_config_sha256": config_hash,
            "wrapper_sha256": wrapper_hash,
            "judge_sha256": judge_hash,
            "resource_budget": runtime.harness.resource_budget.to_dict(),
        }
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", str(request.get(name, "")))
            for name in (
                "candidate_fingerprint",
                "candidate_target_sha256",
                "baseline_fingerprint",
                "fixture_hash",
                "evaluator_hash",
                "sandbox_contract_digest",
                "container_config_digest",
                "image_id_digest",
                "endpoint_digest",
                "executor_config_sha256",
                "wrapper_sha256",
                "judge_sha256",
            )
        ):
            raise RuntimeError("execution envelope binding is incomplete")
        return request

    @staticmethod
    def _validate_execution_metadata(
        metadata: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        """Require a wrapper proof to match the exact immutable request."""

        if not isinstance(metadata, Mapping):
            raise RuntimeError(
                "execution proof is missing: metadata_keys=[]"
            )
        if (
            metadata.get("schema_version") != HOST_SCHEMA_VERSION
            or _safe_text(metadata.get("protocol"), 80) != _SANDBOX_PROTOCOL
        ):
            raise RuntimeError(
                "execution proof protocol changed: "
                + _diagnostic_summary(
                    "schema_present=%s protocol_present=%s metadata_keys=%s"
                    % (
                        "schema_version" in metadata,
                        "protocol" in metadata,
                        sorted(str(key) for key in metadata.keys()),
                    )
                )
            )
        if _safe_text(metadata.get("nonce"), 128) != _safe_text(
            request.get("nonce"), 128
        ):
            raise RuntimeError("execution proof nonce changed")
        if _safe_text(metadata.get("request_digest"), 128).lower() != _digest(
            _canonical(request)
        ):
            raise RuntimeError("execution proof request binding changed")
        for key in (
            "wrapper_sha256",
            "judge_sha256",
            "executor_config_sha256",
            "container_config_digest",
            "image_id_digest",
            "endpoint_digest",
            "sandbox_contract_digest",
        ):
            if _safe_text(metadata.get(key), 128).lower() != _safe_text(
                request.get(key), 128
            ).lower():
                raise RuntimeError("execution proof fixture binding changed")
        if (
            metadata.get("input_integrity_verified") is not True
            or _safe_text(metadata.get("candidate_target_sha256_before"), 128).lower()
            != _safe_text(request.get("candidate_target_sha256"), 128).lower()
            or _safe_text(metadata.get("candidate_target_sha256_after"), 128).lower()
            != _safe_text(request.get("candidate_target_sha256"), 128).lower()
            or _safe_text(metadata.get("judge_sha256_before"), 128).lower()
            != _safe_text(request.get("judge_sha256"), 128).lower()
            or _safe_text(metadata.get("judge_sha256_after"), 128).lower()
            != _safe_text(request.get("judge_sha256"), 128).lower()
        ):
            raise RuntimeError("execution proof input integrity changed")
        if (
            metadata.get("inspect_verified") is not True
            or metadata.get("cleanup_verified") is not True
        ):
            raise RuntimeError("execution proof is not durable")

    def _evaluate_iteration_with_lease(
        self,
        runtime: _Runtime,
        proposal: Any,
        candidate: Any,
        baseline: Any,
        *,
        execution_request: Mapping[str, Any],
        lease_store: OrphanLeaseStore,
    ) -> Any:
        """Evaluate only while the formal Docker nonce has a durable lease."""

        # Canonicalize once before arming or calling the evaluator.  The
        # round-trip creates a deep, immutable-in-practice JSON snapshot so a
        # caller cannot change the request after the nonce lease is armed.
        try:
            request_text = _canonical(dict(execution_request))
            request_snapshot = json.loads(request_text)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("evaluation request is not canonical JSON") from exc
        if not isinstance(request_snapshot, dict):
            raise RuntimeError("evaluation request is not an object")
        request_digest = _digest(_canonical(request_snapshot))

        harness = getattr(runtime, "harness", None)
        expected_candidate_fingerprint = _safe_text(
            getattr(candidate, "fingerprint", None), 128
        ).lower()
        expected_baseline_fingerprint = _safe_text(
            getattr(baseline, "fingerprint", None), 128
        ).lower()
        expected_harness_version = _safe_text(
            getattr(harness, "harness_version", None), 80
        )
        expected_fixture_hash = _safe_text(
            getattr(harness, "fixture_hash", None), 128
        ).lower()
        expected_evaluator_hash = _safe_text(
            getattr(harness, "evaluator_hash", None), 128
        ).lower()
        expected_binding = {
            "candidate_fingerprint": expected_candidate_fingerprint,
            "baseline_fingerprint": expected_baseline_fingerprint,
            "fixture_hash": expected_fixture_hash,
            "evaluator_hash": expected_evaluator_hash,
        }
        if (
            not expected_harness_version
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in expected_binding.values()
            )
        ):
            raise RuntimeError("evaluation lease binding is incomplete")
        request_binding = {
            key: _safe_text(request_snapshot.get(key), 128).lower()
            for key in expected_binding
        }
        if request_binding != expected_binding:
            raise RuntimeError("evaluation request binding changed")

        nonce = _safe_text(request_snapshot.get("nonce"), 128).lower()
        expected_endpoint = _safe_text(
            request_snapshot.get("endpoint_digest"), 128
        ).lower()
        if (
            not re.fullmatch(r"[0-9a-f]{32,128}", nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_endpoint)
        ):
            raise RuntimeError("evaluation lease binding is incomplete")
        image, context, docker = _executor_config_from_fixtures(
            runtime.layout.fixtures
        )
        endpoint = _local_docker_context_endpoint(docker, context)
        if not endpoint or _digest(endpoint) != expected_endpoint:
            raise RuntimeError("evaluation lease endpoint changed")
        lease = lease_store.arm(
            operation="eval",
            nonce=nonce,
            name="brain-memory-p7-eval-" + nonce[:32],
            image=image,
            context=context,
            docker_cli=docker,
            endpoint_digest=expected_endpoint,
            run_id=runtime.layout.run_id,
        )
        completed = False
        process_stopped = False
        primary_error: BaseException | None = None
        try:
            # Give the stem its own deep copy.  Verification below always
            # uses the untouched startup snapshot, even if a faulty seam
            # mutates the mapping it receives.
            stem_request = json.loads(request_text)
            receipt = runtime.stem.evaluate_iteration_proposal(
                proposal,
                candidate,
                baseline,
                host=HostBinding(False),
                execution_request=stem_request,
            )
            # The external lease is a security boundary, so do not trust a
            # duck-typed/mocked result from the stem.  The concrete immutable
            # receipt must authenticate its own hash and explicitly attest
            # that the candidate ran in an isolated tree before any cleanup
            # transition can be sealed.
            from brain.evaluation_harness import EvaluationReceipt

            if type(receipt) is not EvaluationReceipt:
                raise RuntimeError("evaluation returned an invalid receipt type")
            if not receipt.verify():
                raise RuntimeError("evaluation receipt verification failed")
            if receipt.isolated is not True:
                raise RuntimeError("evaluation receipt is not isolated")
            metadata = dict(receipt.metadata or {})
            startup_gates = [
                gate
                for gate in (receipt.gates or ())
                if getattr(gate, "name", "") == "startup"
            ]
            startup_evidence = (
                startup_gates[0].evidence
                if len(startup_gates) == 1
                and isinstance(getattr(startup_gates[0], "evidence", None), Mapping)
                else {}
            )
            raw_exit_code = startup_evidence.get("exit_code")
            raw_timed_out = startup_evidence.get("timed_out")
            startup_gate_passed = bool(
                len(startup_gates) == 1 and getattr(startup_gates[0], "passed", False)
            )
            receipt_binding = {
                "candidate_fingerprint": receipt.candidate_fingerprint.lower(),
                "baseline_fingerprint": (receipt.baseline_fingerprint or "").lower(),
                "fixture_hash": receipt.fixture_hash.lower(),
                "evaluator_hash": receipt.evaluator_hash.lower(),
                "harness_version": receipt.harness_version,
            }
            expected_receipt_binding = dict(expected_binding)
            expected_receipt_binding["harness_version"] = expected_harness_version
            process_stopped = bool(
                len(startup_gates) == 1
                and startup_evidence.get("boundary_closed") is True
                and type(raw_exit_code) is int
                and type(receipt.exit_code) is int
                and raw_exit_code == receipt.exit_code
                and type(raw_timed_out) is bool
                and raw_timed_out is receipt.timed_out
                and _safe_text(
                    startup_evidence.get("execution_request_digest"), 128
                ).lower()
                == request_digest
                and receipt_binding == expected_receipt_binding
            )
            # Preserve an explicit evaluator/wrapper failure.  Failed wrapper
            # JSON is intentionally not an execution proof and commonly has
            # no metadata; validate the proof only for an otherwise successful
            # receipt so the original bounded error is not misreported as a
            # protocol drift.
            receipt_error = _safe_text(receipt.error, 240)
            if (
                receipt.exit_code != 0
                or receipt.timed_out is not False
                or receipt.result_verified is not True
                or receipt_error
                or not startup_gate_passed
                or not process_stopped
            ):
                raise RuntimeError(
                    "evaluation failed: "
                    + _diagnostic_summary(
                        "exit_code=%s timed_out=%s result_verified=%s startup_boundary=%s error=%s"
                        % (
                            receipt.exit_code,
                            receipt.timed_out,
                            receipt.result_verified,
                            process_stopped,
                            receipt_error,
                        )
                    )
                )
            self._validate_execution_metadata(metadata, request_snapshot)
            # A successful lease seal still requires the trusted wrapper proof;
            # timeout receipts remain rejected above even when the host can
            # independently prove that the bounded process boundary closed.
            marked = lease_store.mark_cleanup_pending(
                lease, process_stopped=True
            )
            if marked is not True or not lease_store.complete(lease):
                raise RuntimeError("evaluation cleanup lease is unverified")
            completed = True
            return receipt
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if not completed:
                try:
                    marked = lease_store.mark_cleanup_pending(
                        lease, process_stopped=process_stopped
                    )
                    if marked is not True:
                        raise RuntimeError("evaluation cleanup lease is unverified")
                except BaseException as cleanup_error:
                    if primary_error is not None:
                        try:
                            primary_error.add_note(
                                "cleanup registration failed: "
                                + _diagnostic_summary(str(cleanup_error))
                            )
                        except BaseException:
                            pass
                    else:
                        raise RuntimeError(
                            "evaluation cleanup lease registration failed"
                        ) from cleanup_error

    @staticmethod
    def _receipt_core(receipt: Any) -> dict[str, Any]:
        """Return the immutable evaluation facts that must survive restart."""

        return {
            "accepted": bool(receipt.accepted),
            "candidate_metrics": dict(receipt.candidate_metrics),
            "primary_dimension": _safe_text(receipt.primary_dimension, 120),
            "improvement": receipt.improvement,
            "hard_gates_passed": bool(receipt.hard_gates_passed),
            "failed_gates": list(receipt.failed_gates),
            "result_verified": bool(receipt.result_verified),
            "candidate_fingerprint": _safe_text(
                receipt.candidate_fingerprint, 128
            ).lower(),
            "baseline_fingerprint": _safe_text(
                receipt.baseline_fingerprint, 128
            ).lower(),
            "mode": _safe_text(receipt.mode, 40).lower(),
        }

    @staticmethod
    def _verified_replay_summary(
        probe: Mapping[str, Any],
        *,
        expected_replayable: bool,
        expected_unique: int,
        expected_observation_digest: str | None = None,
    ) -> dict[str, Any]:
        """Validate one post-swap replay and retain only bounded evidence."""

        observation_digest = _safe_text(
            probe.get("observation_digest"), 128
        ).lower()
        execution = probe.get("execution_metadata")
        if (
            probe.get("replayable") is not expected_replayable
            or probe.get("goal_generation_integrity") is not True
            or probe.get("unique_description_count") != expected_unique
            or not re.fullmatch(r"[0-9a-f]{64}", observation_digest)
            or (
                expected_observation_digest is not None
                and observation_digest != expected_observation_digest
            )
            or not isinstance(execution, Mapping)
            or execution.get("input_integrity_verified") is not True
            or execution.get("inspect_verified") is not True
            or execution.get("cleanup_verified") is not True
        ):
            raise RuntimeError("post-swap sandbox replay changed")
        for key in (
            "request_digest",
            "wrapper_sha256",
            "judge_sha256",
            "executor_config_sha256",
            "container_config_digest",
            "image_id_digest",
            "endpoint_digest",
            "sandbox_contract_digest",
            "candidate_target_sha256_before",
            "candidate_target_sha256_after",
            "judge_sha256_before",
            "judge_sha256_after",
        ):
            if not re.fullmatch(
                r"[0-9a-f]{64}", _safe_text(execution.get(key), 128).lower()
            ):
                raise RuntimeError("post-swap execution proof is incomplete")
        return {
            "verified": True,
            "replayable": expected_replayable,
            "goal_generation_integrity": True,
            "unique_description_count": expected_unique,
            "observation_digest": observation_digest,
            "execution_proof_digest": _digest(_canonical(dict(execution))),
        }

    def _prepare_layout(
        self,
        *,
        image_ref: str | None = None,
        context_name: str | None = None,
        docker_cli: str | None = None,
    ) -> RunLayout:
        layout = RunLayout.create(self.run_root)
        try:
            _safe_copy_tree(self.repo_root, layout.active)
            _safe_copy_tree(layout.active, layout.candidate)
            self._write_fixture(
                layout,
                image_ref=image_ref,
                context_name=context_name,
                docker_cli=docker_cli,
            )
        except Exception:
            # The exact newly-created run directory is safe to remove; no
            # caller-owned path is touched.
            shutil.rmtree(layout.root, ignore_errors=True)
            raise
        return layout

    async def prepare_async(self) -> dict[str, Any]:
        try:
            lease_store = self._orphan_lease_store()
            orphan_status = lease_store.sweep()
        except (OSError, TypeError, ValueError, RuntimeError):
            lease_store = None
            orphan_status = {
                "ready": False,
                "pending_count": 0,
                "reaped_count": 0,
                "deferred_count": 0,
                "cleaned_count": 0,
                "reason": "orphan_lease_invalid",
            }
        if not bool(orphan_status.get("ready")):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "phase": "blocked",
                    "reason": _safe_text(orphan_status.get("reason"), 120)
                    or "orphan_cleanup_unverified",
                    "authorization_required": True,
                    "orphan_leases": self._orphan_sweep_public(orphan_status),
                }
            )
        image_ref = _configured_docker_image()
        docker_cli = _configured_docker_cli()
        context_name = _configured_docker_context(docker_cli)
        sandbox = _sandbox_capability_report(
            image_ref,
            context_name,
            docker_cli=docker_cli,
            lease_store=lease_store,
        )
        if not bool(sandbox.get("ready")):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "phase": "blocked",
                    "reason": _safe_text(sandbox.get("reason"), 120)
                    or "external_sandbox_unavailable",
                    "authorization_required": True,
                    "sandbox": sandbox,
                }
            )
        try:
            layout = self._prepare_layout(
                image_ref=image_ref,
                context_name=context_name,
                docker_cli=docker_cli,
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "phase": "blocked",
                    "reason": "run_layout_unavailable",
                    "authorization_required": True,
                }
            )
        runtime: _Runtime | None = None
        restarted: _Runtime | None = None
        stage = "runtime_build"
        last_completed_stage = "startup"
        failed = False
        try:
            runtime = self._build_runtime(layout, sandbox=sandbox)
            last_completed_stage = stage
            stage = "baseline_probe"
            static = static_gap_probe(layout.active)
            replay = _sandbox_replay_probe(
                layout.active,
                layout.fixtures,
                sandbox=sandbox,
                lease_store=lease_store,
                lease_run_id=layout.run_id,
            )
            evidence = {"static": static, "runtime": replay}
            if not static["builtin_hash_detected"] or not static["goal_generator_call_detected"]:
                raise RuntimeError("static evidence does not confirm the selected gap")
            if replay["replayable"]:
                raise RuntimeError("baseline is already replayable; no objective improvement is available")
            last_completed_stage = stage
            stage = "need_record"
            need, need_binding = self._record_attested_need_with_binding(
                runtime, evidence, layout.run_id
            )
            last_completed_stage = stage
            stage = "candidate_generation"
            model_patch, model_reason = await self._generate_and_apply_candidate(
                evidence, active=layout.active, candidate=layout.candidate
            )
            if model_patch is None:
                report = {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": layout.run_id,
                    "phase": "blocked",
                    "reason": model_reason,
                    "reason_code": "candidate_generation_failed",
                    "stage": stage,
                    "last_completed_stage": last_completed_stage,
                    "error_summary": _diagnostic_summary(model_reason),
                    "authorization_required": True,
                    "model": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "generated": False,
                    },
                    "evidence": evidence,
                    "need": self._need_summary(need),
                    "need_binding": need_binding,
                    "continuity": runtime.stem.continuity_readiness_snapshot(),
                    "sandbox": sandbox,
                    "target": self._target_summary(),
                }
                self._save_report(layout, report)
                return public_report(report)
            last_completed_stage = stage
            stage = "evaluation"
            baseline_metrics = {
                "deterministic_replay": 1.0 if replay["replayable"] else 0.0,
                "unique_description_count": float(replay["unique_description_count"]),
                "goal_generation_integrity": 1.0
                if replay["goal_generation_integrity"]
                else 0.0,
            }
            from brain.evaluation_harness import BaselineRevision, CandidateRevision

            baseline = BaselineRevision.from_path(
                layout.active,
                metrics=baseline_metrics,
                revision_id="baseline-" + layout.run_id,
                harness_version=runtime.harness.harness_version,
                fixture_hash=runtime.harness.fixture_hash,
                evaluator_hash=runtime.harness.evaluator_hash,
            )
            candidate = CandidateRevision.from_path(
                layout.candidate, revision_id="candidate-" + layout.run_id
            )
            proposal = runtime.stem.register_iteration_proposal(
                need,
                host=HostBinding(False),
                title=model_patch.title,
                scope=model_patch.scope,
                hypothesis=model_patch.hypothesis,
                evidence=(
                    "static_ast:" + static["evidence_digest"][:24],
                    "runtime_replay:" + replay["observation_digest"][:24],
                ),
                expected_benefits=("deterministic cross-restart goal selection",),
                risks=("goal selection behavior changes only from process-randomized to stable",),
                resource_budget={"timeout_sec": 60, "max_files": 2048},
                rollback_revision=baseline.revision_id,
                baseline_revision=baseline.revision_id,
                candidate_revision=candidate.revision_id,
            )
            execution_request = self._execution_request(
                runtime,
                sandbox,
                candidate_fingerprint=candidate.fingerprint,
                baseline_fingerprint=baseline.fingerprint,
                target_scope=self.target.scope,
            )
            receipt = self._evaluate_iteration_with_lease(
                runtime,
                proposal,
                candidate,
                baseline,
                execution_request=execution_request,
                lease_store=lease_store,
            )
            last_completed_stage = stage
            stage = "continuity"
            lifecycle = await self._start_and_stop(runtime.stem)
            last_completed_stage = stage
            continuity_ready = bool(lifecycle.get("ready"))
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "target": self._target_summary(),
                "run_id": layout.run_id,
                "phase": "awaiting_authorization"
                if receipt.accepted and continuity_ready
                else "blocked",
                "authorization_required": True,
                "model": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "generated": True,
                    "verified": True,
                    "source_patch_digest": model_patch.model_response_hash,
                    "title": model_patch.title,
                    "hypothesis": model_patch.hypothesis,
                    "patch_digest": model_patch.diff_hash,
                    "scope": model_patch.scope,
                },
                "evidence": evidence,
                "need": self._need_summary(need),
                "need_binding": need_binding,
                "baseline": {
                    "revision_id": baseline.revision_id,
                    "fingerprint": baseline.fingerprint,
                    "metrics": baseline_metrics,
                    "metrics_digest": _digest(_canonical(baseline_metrics)),
                },
                "proposal": {
                    "proposal_id": proposal.proposal_id,
                    "title": proposal.title,
                    "scope": proposal.scope,
                    "hypothesis": proposal.hypothesis,
                    "evidence": list(proposal.evidence),
                    "expected_benefits": list(proposal.expected_benefits),
                    "risks": list(proposal.risks),
                    "resource_budget": dict(proposal.resource_budget),
                    "rollback_revision": proposal.rollback_revision,
                    "baseline_revision": proposal.baseline_revision,
                    "candidate_revision": candidate.revision_id,
                    "candidate_fingerprint": candidate.fingerprint,
                },
                "evaluation": self._receipt_summary(receipt),
                "evaluation_execution": dict(receipt.metadata),
                "evaluator_contract": self._evaluator_contract(runtime, sandbox),
                "sandbox": sandbox,
                "continuity": lifecycle,
                "reason": ""
                if receipt.accepted and continuity_ready
                else ("continuity_not_ready" if receipt.accepted else "evaluation_rejected"),
            }
            stage = "persist"
            self._save_report(layout, report)
            return public_report(report)
        except Exception as exc:
            failed = True
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "target": self._target_summary(),
                "run_id": layout.run_id,
                "phase": "blocked",
                "authorization_required": True,
                "reason": type(exc).__name__,
                "reason_code": "prepare_failed",
                "stage": stage,
                "last_completed_stage": last_completed_stage,
                "error_summary": _diagnostic_summary(str(exc)) or type(exc).__name__,
            }
            self._save_report(layout, report)
            return public_report(report)
        finally:
            if runtime is not None:
                try:
                    await runtime.aclose()
                except Exception:
                    if not failed:
                        raise

    def _manifest_payload(
        self,
        layout: RunLayout,
        private_report: Mapping[str, Any],
        public_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build the non-secret data covered by the per-run HMAC."""

        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "run_id": layout.run_id,
            "phase": _safe_text(private_report.get("phase"), 64),
            "private_digest": _digest(_canonical(private_report)),
            "public_digest": _digest(_canonical(public_receipt)),
        }

    def _manifest_mac(self, layout: RunLayout, payload: Mapping[str, Any]) -> str:
        key = _manifest_key_for_run(self._manifest_master_key, layout.run_id)
        return hmac.new(
            key, _canonical(payload).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _write_manifest(
        self,
        layout: RunLayout,
        private_report: Mapping[str, Any],
        public_receipt: Mapping[str, Any],
    ) -> None:
        """Persist only digests and an HMAC outside the exchangeable run tree."""

        payload = self._manifest_payload(layout, private_report, public_receipt)
        manifest = dict(payload)
        manifest["mac"] = self._manifest_mac(layout, payload)
        _write_json(layout.manifest, manifest)

    def _verify_manifest(
        self,
        layout: RunLayout,
        private_report: Mapping[str, Any],
        public_receipt: Mapping[str, Any],
    ) -> None:
        """Verify private/public metadata before any authorization decision."""

        source = layout.manifest
        if _is_link_like(source) or not source.is_file():
            raise ValueError("P7 metadata manifest is unavailable")
        try:
            if source.stat(follow_symlinks=False).st_size > _MANIFEST_MAX_BYTES:
                raise ValueError("P7 metadata manifest exceeds its bounded size")
            manifest = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("P7 metadata manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise ValueError("P7 metadata manifest is invalid")
        unsigned = {
            key: manifest.get(key)
            for key in (
                "schema_version",
                "run_id",
                "phase",
                "private_digest",
                "public_digest",
            )
        }
        supplied_mac = _safe_text(manifest.get("mac"), 128).lower()
        if (
            set(manifest) != set((*unsigned, "mac"))
            or unsigned["schema_version"] != _MANIFEST_SCHEMA_VERSION
            or unsigned["run_id"] != layout.run_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(unsigned["private_digest"]).lower())
            or not re.fullmatch(r"[0-9a-f]{64}", str(unsigned["public_digest"]).lower())
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_mac)
            or not hmac.compare_digest(supplied_mac, self._manifest_mac(layout, unsigned))
        ):
            raise ValueError("P7 metadata manifest authentication failed")
        expected = self._manifest_payload(layout, private_report, public_receipt)
        if expected != unsigned:
            raise ValueError("P7 private/public metadata binding changed")

    def _read_public_receipt(self, layout: RunLayout) -> dict[str, Any]:
        source = layout.metadata
        if _is_link_like(source) or not source.is_file():
            raise ValueError("P7 public run metadata is unavailable")
        try:
            if source.stat(follow_symlinks=False).st_size > _RUN_METADATA_MAX_BYTES:
                raise ValueError("P7 public run metadata exceeds its bounded size")
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("P7 public run metadata is invalid") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != layout.run_id:
            raise ValueError("P7 public run metadata is invalid")
        return payload

    def _save_report(self, layout: RunLayout, report: Mapping[str, Any]) -> None:
        """Persist authorizing evidence and an authenticated public receipt."""

        private_report = dict(report)
        if len(_canonical(private_report).encode("utf-8")) > _RUN_METADATA_MAX_BYTES:
            raise ValueError("P7 run metadata exceeds its bounded size")
        public_receipt = public_report(private_report)
        _write_json(layout.private_metadata, private_report)
        _write_json(layout.metadata, public_receipt)
        self._write_manifest(layout, private_report, public_receipt)

    def _load_metadata(self, layout: RunLayout) -> dict[str, Any]:
        source = layout.private_metadata
        if _is_link_like(source) or not source.is_file():
            raise ValueError("P7 private run metadata is unavailable")
        if source.stat(follow_symlinks=False).st_size > _RUN_METADATA_MAX_BYTES:
            raise ValueError("P7 private run metadata exceeds its bounded size")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("P7 run metadata is invalid") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != layout.run_id:
            raise ValueError("P7 run metadata is invalid")
        public_receipt = self._read_public_receipt(layout)
        if public_receipt != public_report(payload):
            raise ValueError("P7 public run metadata does not match private evidence")
        self._verify_manifest(layout, payload, public_receipt)
        return payload

    def _claim_authorization(
        self, layout: RunLayout
    ) -> tuple[dict[str, Any] | None, str]:
        """Atomically reserve the one authorization for a run.

        The phase transition is itself authenticated by ``_save_report``.  A
        second process therefore observes ``authorizing`` and cannot start a
        competing evaluation while the first process is awaiting/using the
        operator's one-time decision.
        """

        try:
            with _authorization_file_lock(layout.authorization_lock):
                metadata = self._load_metadata(layout)
                phase = _safe_text(metadata.get("phase"), 64)
                if phase != "awaiting_authorization":
                    return None, (
                        "authorization_in_progress"
                        if phase == "authorizing"
                        else "run_is_not_awaiting_authorization"
                    )
                claimed = dict(metadata)
                claimed["phase"] = "authorizing"
                claimed["authorization_used"] = False
                # Bind every later release/finish to this exact claim.  The
                # identifier is private metadata and is covered by the
                # per-run manifest HMAC.
                claimed["authorization_claim_id"] = secrets.token_hex(16)
                claimed["authorization_claimed_at"] = int(time.time())
                self._save_report(layout, claimed)
                return claimed, ""
        except RuntimeError as exc:
            if "authorization lock" in str(exc).lower():
                return None, "authorization_lock_unavailable"
            return None, "metadata_authentication_failed"
        except (OSError, ValueError, TypeError):
            return None, "metadata_authentication_failed"

    def _release_authorization(
        self,
        layout: RunLayout,
        claimed: Mapping[str, Any],
        *,
        reason: str,
        sandbox: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a transient sandbox/config failure to the awaiting state."""

        restored = dict(claimed)
        restored["phase"] = "awaiting_authorization"
        restored["authorization_used"] = False
        restored["reason"] = _safe_text(reason, 120)
        claim_id = _safe_text(claimed.get("authorization_claim_id"), 128)
        if not re.fullmatch(r"[0-9a-f]{32}", claim_id):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": layout.run_id,
                    "phase": "blocked",
                    "reason": "authorization_claim_invalid",
                    "authorization_required": True,
                    "authorization_used": False,
                }
            )
        restored.pop("authorization_claim_id", None)
        restored.pop("authorization_claimed_at", None)
        if sandbox is not None:
            restored["sandbox"] = dict(sandbox)
        try:
            with _authorization_file_lock(layout.authorization_lock):
                current = self._load_metadata(layout)
                if (
                    current.get("phase") != "authorizing"
                    or _safe_text(current.get("authorization_claim_id"), 128)
                    != claim_id
                ):
                    raise RuntimeError("authorization state changed")
                self._save_report(layout, restored)
            return public_report(restored)
        except Exception:
            # Never replace a potentially tampered/advanced manifest with an
            # unauthenticated status report.  The caller receives a bounded
            # blocked result; recovery remains fail-closed.
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": layout.run_id,
                    "phase": "blocked",
                    "reason": "authorization_state_unavailable",
                    "authorization_required": True,
                    "authorization_used": False,
                }
            )

    def _finish_authorization(
        self, layout: RunLayout, report: Mapping[str, Any]
    ) -> None:
        """Commit a terminal authorization result under the same CAS lock."""

        with _authorization_file_lock(layout.authorization_lock):
            current = self._load_metadata(layout)
            if current.get("phase") != "authorizing":
                raise RuntimeError("authorization state changed before commit")
            if report.get("run_id") != layout.run_id:
                raise RuntimeError("authorization result run binding changed")
            if _safe_text(report.get("phase"), 64) not in {"completed", "blocked"}:
                raise RuntimeError("authorization result phase is not terminal")
            if current.get("authorization_used") is True:
                raise RuntimeError("authorization has already been consumed")
            current_claim = _safe_text(current.get("authorization_claim_id"), 128)
            report_claim = _safe_text(report.get("authorization_claim_id"), 128)
            if (
                not re.fullmatch(r"[0-9a-f]{32}", current_claim)
                or report_claim != current_claim
            ):
                raise RuntimeError("authorization claim binding changed")
            # A terminal receipt extends the authenticated prepare evidence;
            # it must not erase the model, evaluator, provenance, or baseline
            # facts needed to audit the one-time authorization later.
            committed = dict(current)
            committed.update(report)
            committed["authorization_used"] = True
            committed.pop("authorization_claim_id", None)
            committed.pop("authorization_claimed_at", None)
            self._save_report(layout, committed)

    async def authorize_and_verify_async(self, run_id: str) -> dict[str, Any]:
        try:
            lease_store = self._orphan_lease_store()
            orphan_status = lease_store.sweep()
        except (OSError, TypeError, ValueError, RuntimeError):
            lease_store = None
            orphan_status = {
                "ready": False,
                "pending_count": 0,
                "reaped_count": 0,
                "deferred_count": 0,
                "cleaned_count": 0,
                "reason": "orphan_lease_invalid",
            }
        if not bool(orphan_status.get("ready")):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": _safe_text(run_id, 32)
                    if re.fullmatch(r"[0-9a-f]{16}", str(run_id or ""))
                    else "",
                    "phase": "blocked",
                    "reason": _safe_text(orphan_status.get("reason"), 120)
                    or "orphan_cleanup_unverified",
                    "authorization_required": True,
                    "orphan_leases": self._orphan_sweep_public(orphan_status),
                }
            )
        try:
            layout = RunLayout.load(run_id, self.run_root)
        except (OSError, TypeError, ValueError):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": _safe_text(run_id, 32)
                    if re.fullmatch(r"[0-9a-f]{16}", str(run_id or ""))
                    else "",
                    "phase": "blocked",
                    "reason": "run_unavailable",
                    "authorization_required": True,
                    "authorization_used": False,
                }
            )
        # Claim the one-time authorization before reading any executable
        # candidate state or probing Docker.  This is the cross-process CAS
        # boundary; every later operation uses the authenticated snapshot it
        # returns rather than a pre-claim read.
        metadata, claim_reason = self._claim_authorization(layout)
        if metadata is None:
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": layout.run_id,
                    "phase": "blocked",
                    "reason": claim_reason or "authorization_claim_failed",
                    "authorization_required": True,
                }
            )
        claim_id = _safe_text(metadata.get("authorization_claim_id"), 128)
        if not re.fullmatch(r"[0-9a-f]{32}", claim_id):
            return public_report(
                {
                    "schema_version": HOST_SCHEMA_VERSION,
                    "run_id": layout.run_id,
                    "phase": "blocked",
                    "reason": "authorization_claim_invalid",
                    "authorization_required": True,
                    "authorization_used": False,
                }
            )
        if not self._target_binding_matches(metadata.get("target")):
            mismatch = {
                "schema_version": HOST_SCHEMA_VERSION,
                "run_id": layout.run_id,
                "phase": "blocked",
                "reason": "target_contract_mismatch",
                "authorization_required": True,
                "authorization_used": True,
                "target": self._target_summary(),
                "authorization_claim_id": claim_id,
            }
            try:
                self._finish_authorization(layout, mismatch)
            except Exception:
                return public_report(
                    {
                        "schema_version": HOST_SCHEMA_VERSION,
                        "run_id": layout.run_id,
                        "phase": "blocked",
                        "reason": "authorization_commit_failed",
                        "authorization_required": True,
                        "authorization_used": False,
                    }
                )
            return public_report(mismatch)
        try:
            image_ref, context_name, docker_cli = _executor_config_from_fixtures(
                layout.fixtures
            )
        except (OSError, ValueError):
            return self._release_authorization(
                layout,
                metadata,
                reason="sandbox_executor_config_invalid",
            )
        sandbox = _sandbox_capability_report(
            image_ref,
            context_name,
            docker_cli=docker_cli,
            lease_store=lease_store,
        )
        if not bool(sandbox.get("ready")):
            # A transient executor outage does not consume the operator's
            # authorization.  Restore the exact pre-claim evidence through
            # the same lock and manifest, never by writing public JSON alone.
            return self._release_authorization(
                layout,
                metadata,
                reason=_safe_text(sandbox.get("reason"), 120)
                or "external_sandbox_unavailable",
                sandbox=sandbox,
            )
        runtime: _Runtime | None = None
        restarted: _Runtime | None = None
        recovery_controller: Any = None
        promotion_pending_rollback = False
        recovery_baseline_fingerprint = ""
        try:
            runtime = self._build_runtime(layout, sandbox=sandbox)
            from brain.evaluation_harness import BaselineRevision, CandidateRevision

            expected_sandbox = metadata.get("sandbox", {})
            if (
                not isinstance(expected_sandbox, Mapping)
                or _safe_text(expected_sandbox.get("contract_digest"), 128).lower()
                != _safe_text(sandbox.get("contract_digest"), 128).lower()
            ):
                raise RuntimeError("sandbox contract changed before authorization")
            evaluator_contract = metadata.get("evaluator_contract", {})
            observed_contract = self._evaluator_contract(runtime, sandbox)
            if (
                not isinstance(evaluator_contract, Mapping)
                or dict(evaluator_contract) != observed_contract
            ):
                raise RuntimeError("evaluator contract changed before authorization")

            baseline_info = metadata.get("baseline", {})
            if not isinstance(baseline_info, Mapping):
                raise RuntimeError("persisted baseline binding is invalid")
            baseline_metrics = baseline_info.get("metrics", {})
            if not isinstance(baseline_metrics, Mapping) or _digest(
                _canonical(baseline_metrics)
            ) != _safe_text(baseline_info.get("metrics_digest"), 128).lower():
                raise RuntimeError("baseline metrics changed before authorization")
            baseline = BaselineRevision.from_path(
                layout.active,
                metrics=baseline_metrics,
                revision_id=_safe_text(baseline_info.get("revision_id"), 160),
                harness_version=runtime.harness.harness_version,
                fixture_hash=runtime.harness.fixture_hash,
                evaluator_hash=runtime.harness.evaluator_hash,
            )
            if baseline.fingerprint != _safe_text(baseline_info.get("fingerprint"), 128):
                raise RuntimeError("active baseline changed before authorization")
            recovery_baseline_fingerprint = baseline.fingerprint
            proposal_info = metadata.get("proposal", {})
            if not isinstance(proposal_info, Mapping):
                raise RuntimeError("persisted proposal binding is invalid")
            candidate = CandidateRevision.from_path(
                layout.candidate,
                revision_id=_safe_text(proposal_info.get("candidate_revision"), 160),
            )
            expected_candidate_fingerprint = _safe_text(
                proposal_info.get("candidate_fingerprint"), 128
            ).lower()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", expected_candidate_fingerprint)
                or candidate.fingerprint != expected_candidate_fingerprint
            ):
                raise RuntimeError("candidate changed after prepare")
            expected_scope = _safe_text(proposal_info.get("scope"), 160).replace("\\", "/")
            if expected_scope != self.target.scope:
                raise RuntimeError("proposal scope changed after prepare")
            model_info = metadata.get("model", {})
            if (
                not isinstance(model_info, Mapping)
                or _safe_text(model_info.get("provider"), 40).lower() != "deepseek"
                    or _safe_text(model_info.get("model"), 80) != "deepseek-v4-flash"
                or model_info.get("generated") is not True
                or model_info.get("verified") is not True
            ):
                raise RuntimeError("model channel binding is invalid")
            expected_patch_digest = _safe_text(
                model_info.get("patch_digest"), 128
            ).lower()
            source_patch_digest = _safe_text(
                model_info.get("source_patch_digest"), 128
            ).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_patch_digest) or not re.fullmatch(
                r"[0-9a-f]{64}", source_patch_digest
            ):
                raise RuntimeError("model patch binding is missing")
            observed_diff = _diff_for_target(layout.active, layout.candidate)
            if _digest(observed_diff) != expected_patch_digest:
                raise RuntimeError("applied model patch changed after prepare")
            _assert_low_risk_transform(layout.active, layout.candidate)
            need_info = metadata.get("need", {})
            binding_info = metadata.get("need_binding", {})
            if not isinstance(need_info, Mapping) or not isinstance(binding_info, Mapping):
                raise RuntimeError("iteration need binding is missing")
            need_data = {
                "need_id": need_info.get("need_id", ""),
                "motive": need_info.get("motive", "growth"),
                "pressure": need_info.get("pressure", 0.0),
                "trigger": need_info.get("trigger", "persistent_pressure"),
                "reason": need_info.get("reason", ""),
                "evidence": need_info.get("evidence", []),
                "source_labels": need_info.get("source_labels", []),
                "urgency": need_info.get("urgency", 0.0),
                "confidence": need_info.get("confidence", 0.0),
                "created_at": need_info.get("created_at", ""),
                "requires_evaluation": True,
                "authorization": False,
            }
            from brain.motivation import IterationNeed, MotivationSourceAttestation

            need = IterationNeed.from_dict(need_data)
            if need is None:
                raise RuntimeError("persisted iteration need is invalid")
            need_payload = self._need_payload(need)
            if _digest(_canonical(need_payload)) != _safe_text(
                binding_info.get("need_digest"), 128
            ).lower():
                raise RuntimeError("iteration need binding changed after prepare")
            proofs = binding_info.get("proofs")
            if not isinstance(proofs, list) or len(proofs) != 4:
                raise RuntimeError("source proof binding is incomplete")
            proof_binding_payload = {"need": need_payload, "proofs": proofs}
            if _digest(_canonical(proof_binding_payload)) != _safe_text(
                binding_info.get("binding_digest"), 128
            ).lower():
                raise RuntimeError("source proof binding changed after prepare")
            persisted_runtime = metadata.get("evidence", {})
            persisted_runtime = (
                persisted_runtime.get("runtime")
                if isinstance(persisted_runtime, Mapping)
                else None
            )
            persisted_execution = (
                persisted_runtime.get("execution_metadata")
                if isinstance(persisted_runtime, Mapping)
                else None
            )
            if (
                not isinstance(persisted_execution, Mapping)
                or persisted_execution.get("inspect_verified") is not True
                or persisted_execution.get("cleanup_verified") is not True
                or _digest(_canonical(dict(persisted_execution)))
                != _safe_text(binding_info.get("execution_proof_digest"), 128).lower()
            ):
                raise RuntimeError("runtime execution proof binding is missing")
            for proof in proofs:
                if not isinstance(proof, Mapping):
                    raise RuntimeError("source proof binding is invalid")
                replay_key = _safe_text(proof.get("replay_key"), 128).lower()
                attestation_hash = _safe_text(
                    proof.get("attestation_hash"), 128
                ).lower()
                event_payload_hash = _safe_text(
                    proof.get("event_payload_hash"), 128
                ).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", replay_key):
                    raise RuntimeError("source proof replay key is invalid")
                if not re.fullmatch(r"[0-9a-f]{64}", attestation_hash) or not re.fullmatch(
                    r"[0-9a-f]{64}", event_payload_hash
                ):
                    raise RuntimeError("source proof hash binding is invalid")
                try:
                    historical = MotivationSourceAttestation.from_dict(
                        proof.get("attestation", {})
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("source proof signature is unavailable") from exc
                source_label = _safe_text(proof.get("source_label"), 40).lower()
                if (
                    historical.attestation_hash != attestation_hash
                    or historical.replay_key != replay_key
                    or runtime.source_attestor.verify_historical(
                        historical,
                        event_id=_safe_text(proof.get("event_id"), 120),
                        payload_hash=event_payload_hash,
                        source_id="p7-source-" + source_label,
                        source_kind="external",
                    )
                    is not True
                ):
                    raise RuntimeError("source proof signature binding changed")
                if runtime.store.is_motivation_attestation_consumed(
                    replay_key=replay_key,
                    attestation_hash=attestation_hash,
                ) is not True:
                    raise RuntimeError("source proof replay receipt is missing")
            proposal_title = _safe_text(
                proposal_info.get("title", model_info.get("title", "")), 180
            )
            proposal_hypothesis = _safe_text(
                proposal_info.get("hypothesis", model_info.get("hypothesis", "")),
                1000,
            )
            proposal_evidence = proposal_info.get("evidence", ())
            proposal_benefits = proposal_info.get("expected_benefits", ())
            proposal_risks = proposal_info.get("risks", ())
            proposal_budget = proposal_info.get("resource_budget", {})
            if not all(
                isinstance(value, (list, tuple))
                for value in (proposal_evidence, proposal_benefits, proposal_risks)
            ) or not isinstance(proposal_budget, Mapping):
                raise RuntimeError("persisted proposal contract is invalid")
            if proposal_title != _safe_text(model_info.get("title"), 180) or proposal_hypothesis != _safe_text(model_info.get("hypothesis"), 1000):
                raise RuntimeError("model proposal binding changed after prepare")
            proposal = runtime.stem.register_iteration_proposal(
                need,
                host=HostBinding(False),
                title=proposal_title,
                scope=self.target.scope,
                hypothesis=proposal_hypothesis,
                evidence=tuple(_safe_text(item, 400) for item in proposal_evidence),
                expected_benefits=tuple(_safe_text(item, 300) for item in proposal_benefits),
                risks=tuple(_safe_text(item, 300) for item in proposal_risks),
                resource_budget=dict(proposal_budget),
                rollback_revision=_safe_text(
                    proposal_info.get("rollback_revision", baseline.revision_id), 180
                ),
                baseline_revision=_safe_text(
                    proposal_info.get("baseline_revision", baseline.revision_id), 180
                ),
                candidate_revision=candidate.revision_id,
            )
            if (
                proposal.rollback_revision != baseline.revision_id
                or proposal.baseline_revision != baseline.revision_id
            ):
                raise RuntimeError("proposal baseline binding changed")
            prepared_evaluation = metadata.get("evaluation", {})
            prepared_execution = metadata.get("evaluation_execution", {})
            if not isinstance(prepared_evaluation, Mapping) or not isinstance(
                prepared_execution, Mapping
            ):
                raise RuntimeError("prepared evaluation proof is missing")
            prepared_proof_digest = _digest(_canonical(dict(prepared_execution)))
            if prepared_proof_digest != _safe_text(
                prepared_evaluation.get("execution_proof_digest"), 128
            ).lower():
                raise RuntimeError("prepared evaluation proof digest changed")
            execution_request = self._execution_request(
                runtime,
                sandbox,
                candidate_fingerprint=candidate.fingerprint,
                baseline_fingerprint=baseline.fingerprint,
                target_scope=self.target.scope,
            )
            receipt = self._evaluate_iteration_with_lease(
                runtime,
                proposal,
                candidate,
                baseline,
                execution_request=execution_request,
                lease_store=lease_store,
            )
            prepared_core = {
                "accepted": bool(prepared_evaluation.get("accepted")),
                "candidate_metrics": dict(
                    prepared_evaluation.get("candidate_metrics", {})
                ),
                "primary_dimension": _safe_text(
                    prepared_evaluation.get("primary_dimension"), 120
                ),
                "improvement": prepared_evaluation.get("improvement"),
                "hard_gates_passed": bool(
                    prepared_evaluation.get("hard_gates_passed")
                ),
                "failed_gates": list(prepared_evaluation.get("failed_gates", [])),
                "result_verified": bool(prepared_evaluation.get("result_verified")),
                "candidate_fingerprint": _safe_text(
                    prepared_evaluation.get("candidate_fingerprint"), 128
                ).lower(),
                "baseline_fingerprint": _safe_text(
                    prepared_evaluation.get("baseline_fingerprint"), 128
                ).lower(),
                "mode": _safe_text(prepared_evaluation.get("mode"), 40).lower(),
            }
            if self._receipt_core(receipt) != prepared_core:
                raise RuntimeError("restarted evaluation facts changed")
            if not receipt.accepted:
                raise RuntimeError("restarted evaluation rejected candidate")
            attestation = runtime.sandbox_attestor.issue_for_receipt(receipt)
            await runtime.stem.start()
            runtime.stem._get_succession_coordinator()
            pre_promotion_continuity = runtime.stem.continuity_readiness_snapshot()
            if not bool(pre_promotion_continuity.get("ready")):
                raise RuntimeError("continuity not ready before promotion")
            promotion = runtime.stem.promote_iteration_proposal(
                proposal,
                candidate,
                receipt,
                host=HostBinding(True),
                authorized=True,
                sandbox_attestation=attestation,
            )
            if not promotion.accepted or promotion.action != "promoted":
                raise RuntimeError("candidate promotion was not committed")
            recovery_controller = runtime.controller
            promotion_pending_rollback = True
            promoted_fingerprint = runtime.controller.active_fingerprint
            if promoted_fingerprint != candidate.fingerprint:
                raise RuntimeError("promoted active tree fingerprint changed")
            await runtime.stem.stop()
            await runtime.aclose()
            runtime = None

            candidate_metrics = dict(receipt.candidate_metrics)
            try:
                expected_candidate_unique = int(
                    candidate_metrics["unique_description_count"]
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("candidate replay metrics are invalid") from exc
            if (
                candidate_metrics.get("deterministic_replay") != 1.0
                or candidate_metrics.get("goal_generation_integrity") != 1.0
            ):
                raise RuntimeError("candidate replay gates changed")
            promoted_probe = _sandbox_replay_probe(
                layout.active,
                layout.fixtures,
                sandbox=sandbox,
                lease_store=lease_store,
                lease_run_id=layout.run_id,
            )
            promoted_replay = self._verified_replay_summary(
                promoted_probe,
                expected_replayable=True,
                expected_unique=expected_candidate_unique,
            )

            # Re-open all durable boundaries in a fresh host object before the
            # rollback rehearsal.  This verifies life ledger/lease restore and
            # controller manifest recovery rather than trusting one process.
            restarted = self._build_runtime(layout, sandbox=sandbox)
            recovery_controller = restarted.controller
            restart_snapshot = await self._start_and_stop(restarted.stem)
            if not bool(restart_snapshot.get("ready")):
                raise RuntimeError("restart continuity is not ready")
            rollback = restarted.controller.rollback(
                authorized=True,
                host=HostBinding(True),
                reason="P7 explicit rollback rehearsal",
            )
            if rollback.accepted:
                promotion_pending_rollback = False
            restored = restarted.controller.active_fingerprint == baseline.fingerprint
            if not rollback.accepted or not restored:
                raise RuntimeError("explicit rollback did not restore the baseline")
            baseline_observation_digest = _safe_text(
                persisted_runtime.get("observation_digest"), 128
            ).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", baseline_observation_digest):
                raise RuntimeError("baseline replay digest is missing")
            try:
                expected_baseline_unique = int(
                    baseline_metrics["unique_description_count"]
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("baseline replay metrics are invalid") from exc
            restored_probe = _sandbox_replay_probe(
                layout.active,
                layout.fixtures,
                sandbox=sandbox,
                lease_store=lease_store,
                lease_run_id=layout.run_id,
            )
            restored_replay = self._verified_replay_summary(
                restored_probe,
                expected_replayable=False,
                expected_unique=expected_baseline_unique,
                expected_observation_digest=baseline_observation_digest,
            )
            # Verify the restored baseline after an explicit restart, while
            # the lease and coordinator are actually active.  A subsequent
            # orderly stop intentionally reports SLEEPING/lease released.
            final_snapshot = await self._start_and_stop(restarted.stem)
            if not bool(final_snapshot.get("ready")):
                raise RuntimeError("post-rollback continuity is not ready")
            await restarted.aclose()
            restarted = None
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "run_id": layout.run_id,
                "phase": "completed"
                if promotion.accepted
                and rollback.accepted
                and restored
                and bool(pre_promotion_continuity.get("ready"))
                and bool(restart_snapshot.get("ready"))
                and bool(final_snapshot.get("ready"))
                else "blocked",
                "authorization_required": True,
                "authorization_used": True,
                "promotion": {
                    "accepted": bool(promotion.accepted),
                    "action": promotion.action,
                    "receipt_hash": promotion.evaluation_receipt_hash,
                    "active_after_digest": promoted_fingerprint,
                    "rollback_available": bool(promotion.rollback_available),
                    "attestation_verified": bool(promotion.attestation_verified),
                },
                "pre_promotion_continuity": pre_promotion_continuity,
                "restart_continuity": restart_snapshot,
                "post_promotion_replay": promoted_replay,
                "rollback": {
                    "accepted": bool(rollback.accepted),
                    "action": rollback.action,
                    "restored_baseline": restored,
                    "rollback_available": bool(rollback.rollback_available),
                },
                "post_rollback_replay": restored_replay,
                "final_continuity": final_snapshot,
                "authorization_claim_id": claim_id,
            }
            self._finish_authorization(layout, report)
            return public_report(report)
        except Exception as exc:
            recovery: dict[str, Any] | None = None
            if (
                promotion_pending_rollback
                and recovery_controller is not None
                and re.fullmatch(r"[0-9a-f]{64}", recovery_baseline_fingerprint)
            ):
                if runtime is not None:
                    try:
                        await runtime.stem.stop()
                    except Exception:
                        pass
                recovery = {
                    "attempted": True,
                    "restored_baseline": False,
                    "action": "rollback_failed",
                }
                try:
                    if (
                        recovery_controller.active_fingerprint
                        == recovery_baseline_fingerprint
                    ):
                        recovery.update(
                            {
                                "restored_baseline": True,
                                "action": "already_restored",
                            }
                        )
                    else:
                        recovery_outcome = recovery_controller.rollback(
                            authorized=True,
                            host=HostBinding(True),
                            reason="P7 failure recovery rollback",
                        )
                        recovery.update(
                            {
                                "restored_baseline": bool(
                                    recovery_outcome.accepted
                                    and recovery_controller.active_fingerprint
                                    == recovery_baseline_fingerprint
                                ),
                                "action": recovery_outcome.action,
                            }
                        )
                except Exception as recovery_exc:
                    recovery["reason"] = type(recovery_exc).__name__
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "run_id": layout.run_id,
                "phase": "blocked",
                "authorization_required": True,
                "authorization_used": True,
                "reason": type(exc).__name__,
                "authorization_claim_id": claim_id,
            }
            if recovery is not None:
                report["recovery_rollback"] = recovery
            try:
                self._finish_authorization(layout, report)
            except Exception:
                # A failed CAS commit is deliberately not overwritten with an
                # unauthenticated status.  The run remains authorizing and
                # requires explicit operator recovery/audit.
                return public_report(
                    {
                        "schema_version": HOST_SCHEMA_VERSION,
                        "run_id": layout.run_id,
                        "phase": "blocked",
                        "authorization_required": True,
                        "authorization_used": False,
                        "reason": "authorization_commit_failed",
                    }
                )
            return public_report(report)
        finally:
            if runtime is not None:
                await runtime.aclose()
            if restarted is not None:
                await restarted.aclose()

    def prepare(self) -> dict[str, Any]:
        return asyncio.run(self.prepare_async())

    def authorize_and_verify(self, run_id: str) -> dict[str, Any]:
        return asyncio.run(self.authorize_and_verify_async(run_id))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="P7 controlled iteration host")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--authorize", metavar="RUN_ID")
    group.add_argument(
        "--inspect-orphans",
        action="store_true",
        help="observation-only orphan lease snapshot; no cleanup or authorization",
    )
    group.add_argument(
        "--prepare-orphan-recovery",
        action="store_true",
        help="seal read-only recovery evidence; requires a real reboot before verification",
    )
    group.add_argument(
        "--verify-orphan-recovery",
        metavar="RECOVERY_ID",
        help="verify sealed evidence after full host restart and nonce-scoped Docker absence",
    )
    args = parser.parse_args()
    recovery_action = args.prepare_orphan_recovery or args.verify_orphan_recovery
    if recovery_action:
        if not os.environ.get(MANIFEST_KEY_ENV):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "evidence_only": True,
                "reason": "manifest_key_unavailable",
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            host = P7ControlledHost()
        except (TypeError, ValueError):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "evidence_only": True,
                "reason": "manifest_key_invalid",
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        except (Exception, KeyboardInterrupt):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "evidence_only": True,
                "reason": "orphan_recovery_registry_invalid",
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            with _authorization_file_lock(host.run_root / ".p7-host.lock"):
                store = host._orphan_lease_store()
                result = (
                    store.prepare_recovery()
                    if args.prepare_orphan_recovery
                    else store.verify_recovery(args.verify_orphan_recovery)
                )
        except (Exception, KeyboardInterrupt):
            result = {
                "phase": "blocked",
                "recovery_id": args.verify_orphan_recovery or "",
                "affected_count": 0,
                "reason": "orphan_recovery_registry_invalid",
            }
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": result.get("phase", "blocked"),
            "evidence_only": True,
            "recovery": result,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("phase") in {"recovery_pending", "no_recovery_needed", "recovery_verified"} else 2
    # Inspect emits only fixed aggregate fields directly; avoid public_report,
    # whose import path loads brain/services and may read .env.
    if args.inspect_orphans:
        if not os.environ.get(MANIFEST_KEY_ENV):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "reason": "manifest_key_unavailable",
                "reason_code": "manifest_key_unavailable",
                "stage": "startup",
                "observation_only": True,
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            host = P7ControlledHost()
        except (TypeError, ValueError):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "reason": "manifest_key_invalid",
                "reason_code": "manifest_key_invalid",
                "stage": "startup",
                "observation_only": True,
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        except (Exception, KeyboardInterrupt):
            report = {
                "schema_version": HOST_SCHEMA_VERSION,
                "phase": "blocked",
                "observation_only": True,
                "orphan_leases": {
                    "inspection_verified": False,
                    "reason": "orphan_lease_registry_invalid",
                },
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            inspection = host._orphan_lease_store().inspect()
        except (Exception, KeyboardInterrupt):
            inspection = {
                "inspection_verified": False,
                "reason": "orphan_lease_registry_invalid",
            }
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": "inspected" if inspection.get("inspection_verified") else "blocked",
            "observation_only": True,
            "orphan_leases": inspection,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if inspection.get("inspection_verified") else 2
    # Separate CLI invocations must share an external manifest key.  The
    # library keeps an ephemeral key only for in-process tests; never present
    # that mode as an authorizable production run.
    if not os.environ.get(MANIFEST_KEY_ENV):
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": "blocked",
            "reason": "manifest_key_unavailable",
            "reason_code": "manifest_key_unavailable",
            "stage": "startup",
            "authorization_required": True,
        }
        print(json.dumps(public_report(report), ensure_ascii=False, sort_keys=True))
        return 2
    try:
        host = P7ControlledHost()
    except (TypeError, ValueError):
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": "blocked",
            "reason": "manifest_key_invalid",
            "reason_code": "manifest_key_invalid",
            "stage": "startup",
            "authorization_required": True,
        }
        print(json.dumps(public_report(report), ensure_ascii=False, sort_keys=True))
        return 2
    try:
        # Serialize CLI operations for this host root.  Per-run authorization
        # still uses its existing lock; this prevents concurrent preparations
        # from consuming duplicate external/model resources.
        with _authorization_file_lock(host.run_root / ".p7-host.lock"):
            report = host.prepare() if args.prepare else host.authorize_and_verify(args.authorize)
    except KeyboardInterrupt:
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": "blocked",
            "reason": "operation_interrupted",
            "reason_code": "operation_interrupted",
            "stage": "cli",
            "authorization_required": True,
        }
    except Exception as exc:
        report = {
            "schema_version": HOST_SCHEMA_VERSION,
            "phase": "blocked",
            "reason": "cli_operation_failed",
            "reason_code": "cli_operation_failed",
            "stage": "cli",
            "error_summary": _diagnostic_summary(str(exc)) or type(exc).__name__,
            "authorization_required": True,
        }
    print(json.dumps(public_report(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.get("phase") in {"awaiting_authorization", "completed"} else 2


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "DEFAULT_TARGET_ID",
    "HOST_SCHEMA_VERSION",
    "ITERATION_TARGETS",
    "IterationTarget",
    "TARGET_SCOPE",
    "ModelPatch",
    "OrphanLeaseHandle",
    "OrphanLeaseStore",
    "P7ControlledHost",
    "RunLayout",
    "coerce_model_patch",
    "public_report",
    "replay_probe",
    "resolve_iteration_target",
    "target_contract_digest",
    "static_gap_probe",
    "validate_model_patch",
]
