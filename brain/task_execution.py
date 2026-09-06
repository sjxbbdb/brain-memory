"""Bounded task planning, execution ledger, and deterministic outcome checks.

This module is deliberately independent from BrainStem and AgentBridge.  It
does not run tools: callers record an action, its observation, and the outcome
returned by a deterministic evaluator.  A result is completed only when there
is reproducible evidence; a bare success flag is not enough.

The records are bounded, JSON-safe, and restartable.  Arguments are retained
as a digest plus a redacted summary, never as raw tool arguments.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


class PlanStatus:
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class StepStatus:
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ActionStatus:
    PLANNED = "planned"
    STARTED = "started"
    OBSERVED = "observed"
    EVALUATED = "evaluated"
    CANCELLED = "cancelled"


class OutcomeQuality:
    VERIFIED = "verified"
    SIMULATED = "simulated"
    FAILED = "failed"
    UNKNOWN = "unknown"
    ALL = frozenset({VERIFIED, SIMULATED, FAILED, UNKNOWN})


OutcomeStatus = OutcomeQuality
ResultQuality = OutcomeQuality

TERMINAL_PLAN_STATUSES = frozenset(
    {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.ABANDONED}
)
TERMINAL_STEP_STATUSES = frozenset(
    {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.ABANDONED}
)

DEFAULT_MAX_PLANS = 32
DEFAULT_MAX_STEPS = 64
DEFAULT_MAX_ACTIONS = 256
DEFAULT_MAX_OBSERVATIONS = 256
DEFAULT_MAX_OUTCOMES = 256
DEFAULT_MAX_REPLANS = 128
DEFAULT_MAX_EVENTS = 512
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_RETRIES = 2

_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|authorization|cookie|credential)\s*[:=]\s*)([^\s,;&]+)",
    re.IGNORECASE,
)
_URL_SECRET_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|password|secret)="
    r")[^&#\s]+",
    re.IGNORECASE,
)


def _safe_int(value: Any, default: int = 0, low: int = 0, high: int = 1000000) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(low, min(high, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return value if math.isfinite(value) else default


def _bool_flag(value: Any, default: bool = False) -> bool:
    """Parse a persisted flag without treating ``"false"`` as truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "verified", "success"}:
            return True
        if normalized in {"0", "false", "no", "off", "unverified", "failure"}:
            return False
    return default


def _text(value: Any, limit: int = 500) -> str:
    return ("" if value is None else str(value)).strip()[: max(1, limit)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: Any, limit: int = 500) -> str:
    value = _text(value, max(1, limit) * 2)
    value = _SECRET_VALUE_RE.sub(r"\1<redacted>", value)
    value = _URL_SECRET_RE.sub(r"\1<redacted>", value)
    return value[: max(1, limit)]


def _safe_value(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Return bounded JSON-safe data and redact common credential fields."""
    if depth > max_depth:
        return "<depth-limit>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        return _redact_text(value, 180)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:16]:
            key = _text(raw_key, 64)
            if not key:
                continue
            result[key] = (
                "<redacted>"
                if _SECRET_KEY_RE.search(key)
                else _safe_value(raw_value, depth + 1, max_depth)
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth + 1, max_depth) for item in list(value)[:16]]
    return _redact_text(repr(value), 180)


def _safe_mapping(value: Any) -> dict[str, Any]:
    value = _safe_value(value)
    return value if isinstance(value, dict) else {}


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raw = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _quality(value: Any, default: str = OutcomeQuality.UNKNOWN) -> str:
    value = _text(value, 24).lower()
    return value if value in OutcomeQuality.ALL else default


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dependencies(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    result: list[str] = []
    for item in list(value)[:32]:
        item = _text(item, 100)
        if item and item not in result:
            result.append(item)
    return result


def _criteria(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = [_text(value, 240)]
        return {"required_text": value} if value[0] else {}
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in list(_safe_mapping(value).items())[:24]}


@dataclass
class ProvenanceRecord:
    source: str = ""
    uri: str = ""
    provider: str = ""
    method: str = ""
    verified: bool = False
    confidence: float = 0.0
    mode: str = ""
    observed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.source = _redact_text(self.source, 160)
        self.uri = _redact_text(self.uri, 300)
        self.provider = _redact_text(self.provider, 100)
        self.method = _redact_text(self.method, 80)
        self.verified = _bool_flag(self.verified)
        self.confidence = max(0.0, min(1.0, _safe_float(self.confidence)))
        self.mode = _redact_text(self.mode, 40).lower()
        self.observed_at = _text(self.observed_at, 80) or _now_iso()
        self.metadata = _safe_mapping(self.metadata)

    @classmethod
    def from_value(cls, value: Any) -> "ProvenanceRecord":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls(source=value if value else "")
        return cls(
            source=value.get("source", value.get("name", "")),
            uri=value.get("uri", value.get("url", "")),
            provider=value.get("provider", ""),
            method=value.get("method", ""),
            verified=value.get("verified", value.get("is_verified", False)),
            confidence=value.get("confidence", 0.0),
            mode=value.get("mode", ""),
            observed_at=value.get("observed_at", value.get("timestamp", "")),
            metadata=value.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "uri": self.uri,
            "provider": self.provider,
            "method": self.method,
            "verified": self.verified,
            "confidence": round(self.confidence, 4),
            "mode": self.mode,
            "observed_at": self.observed_at,
            "metadata": _safe_mapping(self.metadata),
        }


@dataclass
class PlanStep:
    id: str
    plan_id: str
    description: str
    parent_id: str = ""
    depth: int = 0
    dependencies: list[str] = field(default_factory=list)
    status: str = StepStatus.PENDING
    action_type: str = ""
    tool_name: str = ""
    expected: str = ""
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    max_retries: int = DEFAULT_MAX_RETRIES
    attempts: int = 0
    retries: int = 0
    replan_count: int = 0
    created_tick: int = 0
    started_tick: int | None = None
    completed_tick: int | None = None
    last_action_id: str = ""
    result_quality: str = OutcomeQuality.UNKNOWN
    result_summary: str = ""
    blocked_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("step")
        self.plan_id = _text(self.plan_id, 100)
        self.description = _redact_text(self.description, 500)
        self.parent_id = _text(self.parent_id, 100)
        self.depth = _safe_int(self.depth, 0, 0, 100)
        self.dependencies = _dependencies(self.dependencies)
        allowed = {
            StepStatus.PENDING, StepStatus.RUNNING, StepStatus.PAUSED,
            StepStatus.BLOCKED, StepStatus.COMPLETED, StepStatus.FAILED,
            StepStatus.ABANDONED,
        }
        self.status = _text(self.status, 24).lower()
        if self.status not in allowed:
            self.status = StepStatus.PENDING
        self.action_type = _redact_text(self.action_type, 80)
        self.tool_name = _redact_text(self.tool_name, 120)
        self.expected = _redact_text(self.expected, 300)
        self.acceptance_criteria = _criteria(self.acceptance_criteria)
        self.max_retries = _safe_int(self.max_retries, DEFAULT_MAX_RETRIES, 0, 100)
        self.attempts = _safe_int(self.attempts)
        self.retries = _safe_int(self.retries)
        self.replan_count = _safe_int(self.replan_count)
        self.created_tick = _safe_int(self.created_tick)
        if self.started_tick is not None:
            self.started_tick = _safe_int(self.started_tick)
        if self.completed_tick is not None:
            self.completed_tick = _safe_int(self.completed_tick)
        self.last_action_id = _text(self.last_action_id, 100)
        self.result_quality = _quality(self.result_quality)
        self.result_summary = _redact_text(self.result_summary, 400)
        self.blocked_reason = _redact_text(self.blocked_reason, 240)
        self.metadata = _safe_mapping(self.metadata)

    @property
    def can_retry(self) -> bool:
        # ``max_retries`` counts retries after the first attempt.  The ledger
        # increments ``retries`` when an attempt ends, so equality still
        # means one retry remains to be claimed.
        return self.retries <= self.max_retries

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "plan_id": self.plan_id, "description": self.description,
            "parent_id": self.parent_id, "depth": self.depth,
            "dependencies": list(self.dependencies), "status": self.status,
            "action_type": self.action_type, "tool_name": self.tool_name,
            "expected": self.expected,
            "acceptance_criteria": _criteria(self.acceptance_criteria),
            "max_retries": self.max_retries, "attempts": self.attempts,
            "retries": self.retries, "replan_count": self.replan_count,
            "created_tick": self.created_tick, "started_tick": self.started_tick,
            "completed_tick": self.completed_tick,
            "last_action_id": self.last_action_id,
            "result_quality": self.result_quality,
            "result_summary": self.result_summary,
            "blocked_reason": self.blocked_reason,
            "metadata": _safe_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PlanStep | None":
        if not isinstance(data, Mapping):
            return None
        return cls(**{
            "id": data.get("id", ""), "plan_id": data.get("plan_id", ""),
            "description": data.get("description", ""),
            "parent_id": data.get("parent_id", ""), "depth": data.get("depth", 0),
            "dependencies": data.get("dependencies", []), "status": data.get("status", StepStatus.PENDING),
            "action_type": data.get("action_type", ""), "tool_name": data.get("tool_name", ""),
            "expected": data.get("expected", ""),
            "acceptance_criteria": data.get("acceptance_criteria", {}),
            "max_retries": data.get("max_retries", DEFAULT_MAX_RETRIES),
            "attempts": data.get("attempts", 0), "retries": data.get("retries", 0),
            "replan_count": data.get("replan_count", 0),
            "created_tick": data.get("created_tick", 0),
            "started_tick": data.get("started_tick"), "completed_tick": data.get("completed_tick"),
            "last_action_id": data.get("last_action_id", ""),
            "result_quality": data.get("result_quality", OutcomeQuality.UNKNOWN),
            "result_summary": data.get("result_summary", ""),
            "blocked_reason": data.get("blocked_reason", ""),
            "metadata": data.get("metadata", {}),
        })


@dataclass
class TaskPlan:
    id: str
    objective: str
    goal_id: str = ""
    source: str = "user"
    status: str = PlanStatus.PENDING
    max_depth: int = DEFAULT_MAX_DEPTH
    max_steps: int = DEFAULT_MAX_STEPS
    max_retries: int = DEFAULT_MAX_RETRIES
    budget_ticks: int = 0
    deadline_tick: int = 0
    consumed_ticks: int = 0
    created_tick: int = 0
    updated_tick: int = 0
    created_at: str = ""
    active_step_id: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    risk_level: str = "low"
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    revision: int = 0

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("plan")
        self.objective = _redact_text(self.objective, 600)
        self.goal_id = _text(self.goal_id, 100)
        self.source = _redact_text(self.source, 100) or "user"
        allowed = {
            PlanStatus.PENDING, PlanStatus.ACTIVE, PlanStatus.PAUSED,
            PlanStatus.BLOCKED, PlanStatus.COMPLETED, PlanStatus.FAILED,
            PlanStatus.ABANDONED,
        }
        self.status = _text(self.status, 24).lower()
        if self.status not in allowed:
            self.status = PlanStatus.PENDING
        self.max_depth = _safe_int(self.max_depth, DEFAULT_MAX_DEPTH, 0, 32)
        self.max_steps = _safe_int(self.max_steps, DEFAULT_MAX_STEPS, 1, 10000)
        self.max_retries = _safe_int(self.max_retries, DEFAULT_MAX_RETRIES, 0, 100)
        self.budget_ticks = _safe_int(self.budget_ticks)
        self.deadline_tick = _safe_int(self.deadline_tick)
        self.consumed_ticks = _safe_int(self.consumed_ticks)
        self.created_tick = _safe_int(self.created_tick)
        self.updated_tick = _safe_int(self.updated_tick)
        self.created_at = _text(self.created_at, 80) or _now_iso()
        self.active_step_id = _text(self.active_step_id, 100)
        self.risk_level = _redact_text(self.risk_level, 40).lower() or "low"
        self.constraints = _safe_mapping(self.constraints)
        self.metadata = _safe_mapping(self.metadata)
        self.revision = _safe_int(self.revision)
        self.steps = [s for s in (self.steps if isinstance(self.steps, list) else []) if isinstance(s, PlanStep)]

    def get_step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == _text(step_id, 100)), None)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_PLAN_STATUSES

    @property
    def completed_steps(self) -> int:
        return sum(1 for step in self.steps if step.status == StepStatus.COMPLETED)

    @property
    def progress(self) -> float:
        return round(self.completed_steps / max(1, len(self.steps)), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "objective": self.objective, "goal_id": self.goal_id,
            "source": self.source, "status": self.status,
            "max_depth": self.max_depth, "max_steps": self.max_steps,
            "max_retries": self.max_retries, "budget_ticks": self.budget_ticks,
            "deadline_tick": self.deadline_tick, "consumed_ticks": self.consumed_ticks,
            "created_tick": self.created_tick, "updated_tick": self.updated_tick,
            "created_at": self.created_at, "active_step_id": self.active_step_id,
            "steps": [s.to_dict() for s in self.steps[-self.max_steps:]],
            "risk_level": self.risk_level, "constraints": _safe_mapping(self.constraints),
            "metadata": _safe_mapping(self.metadata), "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TaskPlan | None":
        if not isinstance(data, Mapping):
            return None
        raw_steps = data.get("steps", [])
        steps = []
        if isinstance(raw_steps, list):
            steps = [s for s in (PlanStep.from_dict(x) for x in raw_steps) if s is not None]
        plan = cls(
            id=data.get("id", ""), objective=data.get("objective", ""),
            goal_id=data.get("goal_id", ""), source=data.get("source", "user"),
            status=data.get("status", PlanStatus.PENDING),
            max_depth=data.get("max_depth", DEFAULT_MAX_DEPTH),
            max_steps=data.get("max_steps", DEFAULT_MAX_STEPS),
            max_retries=data.get("max_retries", DEFAULT_MAX_RETRIES),
            budget_ticks=data.get("budget_ticks", 0),
            deadline_tick=data.get("deadline_tick", 0),
            consumed_ticks=data.get("consumed_ticks", 0),
            created_tick=data.get("created_tick", 0),
            updated_tick=data.get("updated_tick", 0),
            created_at=data.get("created_at", ""), active_step_id=data.get("active_step_id", ""),
            steps=steps, risk_level=data.get("risk_level", "low"),
            constraints=data.get("constraints", {}), metadata=data.get("metadata", {}),
            revision=data.get("revision", 0),
        )
        unique_steps: list[PlanStep] = []
        seen_step_ids: set[str] = set()
        for step in plan.steps:
            if step.plan_id not in ("", plan.id) or step.id in seen_step_ids:
                continue
            seen_step_ids.add(step.id)
            unique_steps.append(step)
        plan.steps = unique_steps[:plan.max_steps]
        for step in plan.steps:
            step.plan_id = plan.id
        if plan.active_step_id and plan.get_step(plan.active_step_id) is None:
            plan.active_step_id = ""
        return plan


@dataclass
class ActionRecord:
    id: str
    plan_id: str
    step_id: str
    action_type: str = ""
    tool_name: str = ""
    args_digest: str = ""
    args_summary: str = ""
    expected: str = ""
    tick: int = 0
    attempt_no: int = 1
    status: str = ActionStatus.PLANNED
    episode_id: str = ""
    created_at: str = ""
    started_tick: int | None = None
    observed_ids: list[str] = field(default_factory=list)
    outcome_id: str = ""

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("action")
        self.plan_id = _text(self.plan_id, 100)
        self.step_id = _text(self.step_id, 100)
        self.action_type = _redact_text(self.action_type, 80)
        self.tool_name = _redact_text(self.tool_name, 120)
        self.args_digest = _text(self.args_digest, 128)
        self.args_summary = _redact_text(self.args_summary, 600)
        self.expected = _redact_text(self.expected, 300)
        self.tick = _safe_int(self.tick)
        self.attempt_no = _safe_int(self.attempt_no, 1, 1)
        self.status = _text(self.status, 24).lower()
        if self.status not in {
            ActionStatus.PLANNED, ActionStatus.STARTED, ActionStatus.OBSERVED,
            ActionStatus.EVALUATED, ActionStatus.CANCELLED,
        }:
            self.status = ActionStatus.PLANNED
        self.episode_id = _text(self.episode_id, 100)
        self.created_at = _text(self.created_at, 80) or _now_iso()
        if self.started_tick is not None:
            self.started_tick = _safe_int(self.started_tick)
        self.observed_ids = _dependencies(self.observed_ids)[-16:]
        self.outcome_id = _text(self.outcome_id, 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "plan_id": self.plan_id, "step_id": self.step_id,
            "action_type": self.action_type, "tool_name": self.tool_name,
            "args_digest": self.args_digest, "args_summary": self.args_summary,
            "expected": self.expected, "tick": self.tick,
            "attempt_no": self.attempt_no, "status": self.status,
            "episode_id": self.episode_id, "created_at": self.created_at,
            "started_tick": self.started_tick, "observed_ids": list(self.observed_ids),
            "outcome_id": self.outcome_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ActionRecord | None":
        if not isinstance(data, Mapping):
            return None
        return cls(**{
            "id": data.get("id", ""), "plan_id": data.get("plan_id", ""),
            "step_id": data.get("step_id", ""), "action_type": data.get("action_type", ""),
            "tool_name": data.get("tool_name", ""), "args_digest": data.get("args_digest", ""),
            "args_summary": data.get("args_summary", ""), "expected": data.get("expected", ""),
            "tick": data.get("tick", 0), "attempt_no": data.get("attempt_no", 1),
            "status": data.get("status", ActionStatus.PLANNED),
            "episode_id": data.get("episode_id", ""), "created_at": data.get("created_at", ""),
            "started_tick": data.get("started_tick"), "observed_ids": data.get("observed_ids", []),
            "outcome_id": data.get("outcome_id", ""),
        })


@dataclass
class ObservationRecord:
    id: str
    action_id: str
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    provenance: ProvenanceRecord = field(default_factory=ProvenanceRecord)
    tick: int = 0
    simulated: bool = False
    error: str = ""
    created_at: str = ""

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("observation")
        self.action_id = _text(self.action_id, 100)
        self.summary = _redact_text(self.summary, 600)
        self.data = _safe_mapping(self.data)
        self.provenance = ProvenanceRecord.from_value(self.provenance)
        self.tick = _safe_int(self.tick)
        self.simulated = _bool_flag(self.simulated)
        self.error = _redact_text(self.error, 300)
        self.created_at = _text(self.created_at, 80) or _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "action_id": self.action_id, "summary": self.summary,
            "data": _safe_mapping(self.data), "provenance": self.provenance.to_dict(),
            "tick": self.tick, "simulated": self.simulated, "error": self.error,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ObservationRecord | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            id=data.get("id", ""), action_id=data.get("action_id", ""),
            summary=data.get("summary", ""), data=data.get("data", {}),
            provenance=data.get("provenance", {}), tick=data.get("tick", 0),
            simulated=data.get("simulated", False), error=data.get("error", ""),
            created_at=data.get("created_at", ""),
        )


@dataclass
class EvaluationResult:
    status: str
    success: bool | None
    reason: str
    confidence: float = 0.0
    checks: dict[str, Any] = field(default_factory=dict)
    evaluator: str = "deterministic"

    def __post_init__(self):
        self.status = _quality(self.status)
        self.success = self.success if isinstance(self.success, bool) else None
        self.reason = _redact_text(self.reason, 400)
        self.confidence = max(0.0, min(1.0, _safe_float(self.confidence)))
        self.checks = _safe_mapping(self.checks)
        self.evaluator = _redact_text(self.evaluator, 80) or "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "success": self.success, "reason": self.reason,
            "confidence": round(self.confidence, 4), "checks": _safe_mapping(self.checks),
            "evaluator": self.evaluator,
        }


@dataclass
class OutcomeRecord:
    id: str
    action_id: str
    plan_id: str
    step_id: str
    status: str = OutcomeQuality.UNKNOWN
    success: bool | None = None
    summary: str = ""
    reason: str = ""
    confidence: float = 0.0
    evaluator: str = "deterministic"
    observation_id: str = ""
    tick: int = 0
    checks: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("outcome")
        self.action_id = _text(self.action_id, 100)
        self.plan_id = _text(self.plan_id, 100)
        self.step_id = _text(self.step_id, 100)
        self.status = _quality(self.status)
        self.success = self.success if isinstance(self.success, bool) else None
        self.summary = _redact_text(self.summary, 600)
        self.reason = _redact_text(self.reason, 400)
        self.confidence = max(0.0, min(1.0, _safe_float(self.confidence)))
        self.evaluator = _redact_text(self.evaluator, 80) or "deterministic"
        self.observation_id = _text(self.observation_id, 100)
        self.tick = _safe_int(self.tick)
        self.checks = _safe_mapping(self.checks)
        self.created_at = _text(self.created_at, 80) or _now_iso()

    @property
    def quality(self) -> str:
        return self.status

    @property
    def terminal(self) -> bool:
        return self.status in {OutcomeQuality.VERIFIED, OutcomeQuality.FAILED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "action_id": self.action_id, "plan_id": self.plan_id,
            "step_id": self.step_id, "status": self.status, "quality": self.status,
            "success": self.success, "summary": self.summary, "reason": self.reason,
            "confidence": round(self.confidence, 4), "evaluator": self.evaluator,
            "observation_id": self.observation_id, "tick": self.tick,
            "checks": _safe_mapping(self.checks), "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "OutcomeRecord | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            id=data.get("id", ""), action_id=data.get("action_id", ""),
            plan_id=data.get("plan_id", ""), step_id=data.get("step_id", ""),
            status=data.get("status", data.get("quality", OutcomeQuality.UNKNOWN)),
            success=data.get("success") if isinstance(data.get("success"), bool) else None,
            summary=data.get("summary", ""), reason=data.get("reason", ""),
            confidence=data.get("confidence", 0.0), evaluator=data.get("evaluator", "deterministic"),
            observation_id=data.get("observation_id", ""), tick=data.get("tick", 0),
            checks=data.get("checks", {}), created_at=data.get("created_at", ""),
        )


@dataclass
class ReplanProposal:
    id: str
    plan_id: str
    step_id: str
    reason: str
    alternative: dict[str, Any] = field(default_factory=dict)
    safe: bool = True
    requires_approval: bool = False
    approved: bool = False
    status: str = "proposed"
    tick: int = 0
    created_at: str = ""

    def __post_init__(self):
        self.id = _text(self.id, 100) or _new_id("replan")
        self.plan_id = _text(self.plan_id, 100)
        self.step_id = _text(self.step_id, 100)
        self.reason = _redact_text(self.reason, 400)
        self.alternative = _safe_mapping(self.alternative)
        self.safe = _bool_flag(self.safe)
        self.requires_approval = _bool_flag(self.requires_approval)
        self.approved = _bool_flag(self.approved)
        self.status = _redact_text(self.status, 24).lower() or "proposed"
        self.tick = _safe_int(self.tick)
        self.created_at = _text(self.created_at, 80) or _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "plan_id": self.plan_id, "step_id": self.step_id,
            "reason": self.reason, "alternative": _safe_mapping(self.alternative),
            "safe": self.safe, "requires_approval": self.requires_approval,
            "approved": self.approved, "status": self.status, "tick": self.tick,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ReplanProposal | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            id=data.get("id", ""), plan_id=data.get("plan_id", ""),
            step_id=data.get("step_id", ""), reason=data.get("reason", ""),
            alternative=data.get("alternative", {}), safe=data.get("safe", True),
            requires_approval=data.get("requires_approval", False),
            approved=data.get("approved", False), status=data.get("status", "proposed"),
            tick=data.get("tick", 0), created_at=data.get("created_at", ""),
        )


class DeterministicOutcomeEvaluator:
    """Evaluate observations with repeatable checks only; never invoke an LLM."""

    @staticmethod
    def _parts(observation: Any) -> tuple[str, dict[str, Any], ProvenanceRecord, bool, str]:
        if isinstance(observation, ObservationRecord):
            return observation.summary, observation.data, observation.provenance, observation.simulated, observation.error
        if not isinstance(observation, Mapping):
            return _redact_text(observation, 600), {}, ProvenanceRecord(), False, ""
        summary = observation.get("summary", observation.get("text", observation.get("result", "")))
        data = observation.get("data", observation.get("payload", {}))
        if not isinstance(data, Mapping):
            data = {
                k: v for k, v in observation.items()
                if k not in {"summary", "text", "result", "provenance", "simulated", "error"}
            }
        return (
            _redact_text(summary, 600),
            _safe_mapping(data),
            ProvenanceRecord.from_value(observation.get("provenance", {})),
            _bool_flag(observation.get("simulated", False)),
            _redact_text(observation.get("error", ""), 300),
        )

    @staticmethod
    def _lookup(data: Mapping[str, Any], path: str) -> tuple[bool, Any]:
        current: Any = data
        for part in _text(path, 120).split("."):
            if not part or not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
        return True, current

    @staticmethod
    def _status_code(data: Mapping[str, Any]) -> int | None:
        for key in ("status_code", "http_status", "status"):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if 100 <= value <= 599:
                return value
        return None

    def evaluate(
        self,
        observation: Any,
        criteria: Mapping[str, Any] | None = None,
        *,
        explicit_success: bool | None = None,
        explicit_quality: str | None = None,
    ) -> EvaluationResult:
        summary, data, provenance, simulated, error = self._parts(observation)
        rules = _criteria(criteria)
        lower = summary.casefold()
        checks: dict[str, Any] = {}
        status_code = self._status_code(data)
        failed = bool(error) or bool(data.get("error")) or data.get("ok") is False or data.get("success") is False
        if status_code is not None and status_code >= 400:
            failed = True

        required_text = rules.get("required_text", rules.get("contains", []))
        if isinstance(required_text, str):
            required_text = [required_text]
        if isinstance(required_text, Sequence) and not isinstance(required_text, (bytes, bytearray)):
            for i, raw in enumerate(list(required_text)[:16]):
                item = _text(raw, 240).casefold()
                checks[f"required_text[{i}]"] = bool(item and item in lower)
                if item and item not in lower:
                    failed = True

        forbidden_text = rules.get("forbidden_text", rules.get("excludes", []))
        if isinstance(forbidden_text, str):
            forbidden_text = [forbidden_text]
        if isinstance(forbidden_text, Sequence) and not isinstance(forbidden_text, (bytes, bytearray)):
            for i, raw in enumerate(list(forbidden_text)[:16]):
                item = _text(raw, 240).casefold()
                checks[f"forbidden_text[{i}]"] = bool(item and item not in lower)
                if item and item in lower:
                    failed = True

        required_fields = rules.get("required_fields", rules.get("fields", []))
        if isinstance(required_fields, str):
            required_fields = [required_fields]
        if isinstance(required_fields, Sequence) and not isinstance(required_fields, (bytes, bytearray)):
            for i, raw in enumerate(list(required_fields)[:24]):
                found, _ = self._lookup(data, _text(raw, 120))
                checks[f"required_fields[{i}]"] = found
                if not found:
                    failed = True

        # At least one of these fields must be present.  This is useful for
        # compatible read-only tools whose payloads use ``memories`` or
        # ``results`` interchangeably.
        any_fields = rules.get("any_fields", rules.get("required_any_fields", []))
        if isinstance(any_fields, str):
            any_fields = [any_fields]
        if isinstance(any_fields, Sequence) and not isinstance(any_fields, (bytes, bytearray)):
            candidates = [_text(item, 120) for item in list(any_fields)[:24] if _text(item, 120)]
            if candidates:
                found_any = any(self._lookup(data, item)[0] for item in candidates)
                checks["any_fields"] = found_any
                if not found_any:
                    failed = True

        # Collection cardinality is checked without retaining the collection
        # body in the ledger.
        min_count = rules.get("min_count")
        if min_count is not None:
            try:
                threshold = max(0, int(min_count))
            except (TypeError, ValueError, OverflowError):
                threshold = 0
            count_value = data.get("count")
            if count_value is None:
                for key in ("results", "memories", "items"):
                    if isinstance(data.get(key), Sequence) and not isinstance(data.get(key), (str, bytes, bytearray)):
                        count_value = len(data[key])
                        break
            try:
                observed_count = int(count_value) if count_value is not None else None
            except (TypeError, ValueError, OverflowError):
                observed_count = None
            checks["min_count"] = observed_count >= threshold if observed_count is not None else None
            if observed_count is not None and observed_count < threshold:
                failed = True

        if rules.get("provenance_required"):
            # The data payload is produced by the observed tool and may only
            # assert its own trustworthiness.  A provenance requirement is
            # satisfied solely by the separate provenance envelope created
            # at the trusted bridge boundary; fields such as
            # ``provenance_verified`` or ``verified_sources`` are metadata,
            # not self-authenticating proof.
            provenance_ok = bool(provenance.verified)
            checks["provenance_required"] = provenance_ok
            if not provenance_ok:
                failed = True

        allowed_statuses = rules.get("status_codes", rules.get("expected_status"))
        if isinstance(allowed_statuses, (str, int, float)):
            allowed_statuses = [allowed_statuses]
        if isinstance(allowed_statuses, Sequence) and not isinstance(allowed_statuses, (bytes, bytearray)):
            codes = set()
            for raw in list(allowed_statuses)[:16]:
                try:
                    codes.add(int(raw))
                except (TypeError, ValueError, OverflowError):
                    pass
            if codes:
                checks["status_code"] = status_code in codes if status_code is not None else None
                if status_code is not None and status_code not in codes:
                    failed = True

        if "exists" in rules:
            expected = bool(rules.get("exists"))
            observed = data.get("exists", data.get("path_exists"))
            if observed is None and "path" in rules:
                observed, _ = self._lookup(data, _text(rules.get("path"), 120))
            checks["exists"] = (bool(observed) == expected) if observed is not None else None
            if observed is not None and bool(observed) != expected:
                failed = True

        values = rules.get("value_equals")
        if isinstance(values, Mapping):
            for path, expected in list(values.items())[:16]:
                found, actual = self._lookup(data, _text(path, 120))
                matches = found and actual == expected
                checks[f"value_equals.{_text(path, 120)}"] = matches
                if found and not matches:
                    failed = True

        allowed_sources = rules.get("allowed_sources")
        if isinstance(allowed_sources, str):
            allowed_sources = [allowed_sources]
        if isinstance(allowed_sources, Sequence) and not isinstance(allowed_sources, (bytes, bytearray)):
            allowed = [_text(x, 160).casefold() for x in list(allowed_sources)[:16] if _text(x, 160)]
            source_text = " ".join((provenance.source, provenance.provider, provenance.uri)).casefold()
            if allowed:
                checks["allowed_sources"] = bool(source_text and any(x in source_text for x in allowed))
                if not source_text or not any(x in source_text for x in allowed):
                    failed = True

        if "min_confidence" in rules:
            threshold = max(0.0, min(1.0, _safe_float(rules.get("min_confidence"))))
            checks["min_confidence"] = provenance.confidence >= threshold
            if provenance.confidence < threshold:
                failed = True

        requested = _quality(explicit_quality, "") if explicit_quality else ""
        if failed or requested == OutcomeQuality.FAILED or explicit_success is False:
            return EvaluationResult(OutcomeQuality.FAILED, False, error or "确定性验收条件未满足", 0.95, checks)

        simulated = simulated or provenance.mode in {"simulated", "dry_run", "dry-run"} or data.get("simulated") is True or data.get("dry_run") is True
        if requested == OutcomeQuality.SIMULATED:
            simulated = True
        if simulated:
            return EvaluationResult(OutcomeQuality.SIMULATED, None, "观察来自模拟/试运行，尚未证明真实成果", 0.7, checks)

        unknown = any(v is None for v in checks.values())
        failed_check = any(v is False for v in checks.values())
        assertion_fields = {
            "verified", "success", "succeeded", "ok",
            "provenance_verified", "verified_sources",
        }
        assertion_only_checks: set[str] = set()
        for field_name in ("required_fields", "fields"):
            raw_fields = rules.get(field_name, [])
            if isinstance(raw_fields, str):
                raw_fields = [raw_fields]
            if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, (bytes, bytearray)):
                for index, raw in enumerate(list(raw_fields)[:24]):
                    name = _text(raw, 120).split(".")[-1].lower()
                    if name in assertion_fields:
                        assertion_only_checks.add(f"required_fields[{index}]")
        raw_any_fields = rules.get(
            "any_fields", rules.get("required_any_fields", [])
        )
        if isinstance(raw_any_fields, str):
            raw_any_fields = [raw_any_fields]
        if isinstance(raw_any_fields, Sequence) and not isinstance(
            raw_any_fields, (bytes, bytearray)
        ):
            any_names = {
                _text(raw, 120).split(".")[-1].lower()
                for raw in list(raw_any_fields)[:24]
                if _text(raw, 120)
            }
            if any_names and any_names.issubset(assertion_fields):
                assertion_only_checks.add("any_fields")
        raw_equals = rules.get("value_equals")
        if isinstance(raw_equals, Mapping):
            for path in list(raw_equals.keys())[:16]:
                normalized_path = _text(path, 120)
                if normalized_path.split(".")[-1].lower() in assertion_fields:
                    assertion_only_checks.add(f"value_equals.{normalized_path}")
        claim_only_checks = bool(checks) and set(checks).issubset(
            assertion_only_checks
        )
        # ``verified`` in a tool payload is only an assertion by the producer;
        # it is not proof by itself.  Trust comes from an explicit provenance
        # record (normally constructed by the bridge) or from one or more
        # deterministic checks that can be reproduced from the bounded data.
        # This prevents a caller from closing a task by sending
        # ``{"data": {"verified": true}}`` with no evidence.
        proof = bool(provenance.verified)
        proof = proof or (bool(checks) and not unknown and not failed_check)
        if claim_only_checks and not provenance.verified:
            return EvaluationResult(
                OutcomeQuality.UNKNOWN,
                None,
                "仅有自我声明字段，不能作为验证依据",
                provenance.confidence,
                checks,
            )
        # A caller-provided success/quality flag is metadata, not evidence.
        # Even ``quality=verified`` must be backed by a provenance marker or
        # a passing deterministic check; otherwise the result remains
        # unknown and cannot advance a plan.
        if proof and not unknown and not failed_check:
            return EvaluationResult(OutcomeQuality.VERIFIED, True, "确定性条件通过且存在可验证来源", max(0.8, provenance.confidence), checks)
        return EvaluationResult(OutcomeQuality.UNKNOWN, None, "缺少足够的确定性证据，不能确认成果", provenance.confidence, checks)


# ---------------------------------------------------------------------------
# Bounded plan and execution ledger
# ---------------------------------------------------------------------------


class TaskExecutionLedger:
    """Own plans and the causal action -> observation -> outcome ledger.

    The ledger is synchronous and side-effect free. A host process may persist
    snapshot() together with its existing state tree; this class never runs a
    tool and never treats a bare success flag as proof.
    """

    SAFE_READ_TOOLS = frozenset({"memory_search", "web_search", "file_read"})

    def __init__(
        self,
        max_plans: int = DEFAULT_MAX_PLANS,
        max_steps_per_plan: int = DEFAULT_MAX_STEPS,
        max_actions: int = DEFAULT_MAX_ACTIONS,
        max_observations: int = DEFAULT_MAX_OBSERVATIONS,
        max_outcomes: int = DEFAULT_MAX_OUTCOMES,
        max_replans: int = DEFAULT_MAX_REPLANS,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        default_max_retries: int = DEFAULT_MAX_RETRIES,
        evaluator: DeterministicOutcomeEvaluator | None = None,
    ):
        self.max_plans = _safe_int(max_plans, DEFAULT_MAX_PLANS, 1, 1000)
        self.max_steps_per_plan = _safe_int(max_steps_per_plan, DEFAULT_MAX_STEPS, 1, 10000)
        self.max_actions = _safe_int(max_actions, DEFAULT_MAX_ACTIONS, 1, 100000)
        self.max_observations = _safe_int(max_observations, DEFAULT_MAX_OBSERVATIONS, 1, 100000)
        self.max_outcomes = _safe_int(max_outcomes, DEFAULT_MAX_OUTCOMES, 1, 100000)
        self.max_replans = _safe_int(max_replans, DEFAULT_MAX_REPLANS, 1, 100000)
        self.max_events = _safe_int(max_events, DEFAULT_MAX_EVENTS, 1, 100000)
        self.max_depth = _safe_int(max_depth, DEFAULT_MAX_DEPTH, 0, 32)
        self.default_max_retries = _safe_int(default_max_retries, DEFAULT_MAX_RETRIES, 0, 100)
        self.evaluator = evaluator or DeterministicOutcomeEvaluator()
        # Policy flags are data-only. BrainStem sets them from configuration;
        # standalone ledger use therefore also fails closed by default.
        self.require_verified_completion = True
        self.auto_replan = False
        self.plans: dict[str, TaskPlan] = {}
        self.actions: dict[str, ActionRecord] = {}
        self.observations: dict[str, ObservationRecord] = {}
        self.outcomes: dict[str, OutcomeRecord] = {}
        self.replans: dict[str, ReplanProposal] = {}
        self.events: list[dict[str, Any]] = []
        self._order = {
            key: [] for key in ("plans", "actions", "observations", "outcomes", "replans")
        }
        self.stats: dict[str, int] = {
            "plans_created": 0, "steps_added": 0, "actions_recorded": 0,
            "observations_recorded": 0, "outcomes_recorded": 0,
            "verified_outcomes": 0, "simulated_outcomes": 0,
            "failed_outcomes": 0, "unknown_outcomes": 0,
            "retries_requested": 0, "replans_proposed": 0,
            "replans_applied": 0, "capacity_rejections": 0,
            "lifecycle_rejections": 0,
            "recovered_actions": 0, "false_success_prevented": 0,
        }

    @staticmethod
    def _args_summary(args: Any) -> str:
        """Describe argument names only; never persist argument values."""
        if not isinstance(args, Mapping):
            return ""
        names: list[str] = []
        for raw_key in list(args.keys())[:24]:
            key = _text(raw_key, 64)
            if key and key not in names:
                names.append(key)
        return "keys=" + ",".join(names)[:560] if names else ""

    def _store(self, collection: str, key: str, value: Any, limit: int) -> bool:
        store = getattr(self, collection)
        order = self._order[collection]
        if key in store:
            # All journal IDs are immutable causal keys.  Callers that want
            # idempotency must compare the existing record before reaching
            # this helper; silently replacing a record here would orphan its
            # observations/outcome or let a reused plan ID cross goals.
            return False
        if len(order) >= limit:
            candidate = None
            for old_id in order:
                old = store.get(old_id)
                if collection == "plans":
                    evictable = old is not None and old.status in TERMINAL_PLAN_STATUSES
                elif collection == "actions":
                    evictable = old is not None and old.status in {
                        ActionStatus.EVALUATED, ActionStatus.CANCELLED
                    }
                elif collection == "replans":
                    evictable = old is not None and old.status in {
                        "applied", "rejected", "expired", "requires_operator"
                    }
                else:
                    evictable = old is not None
                # A bounded journal may discard unreferenced history, but it
                # must never break a durable causal chain.  In particular,
                # retaining an outcome without its action/observation (or a
                # plan without the records that prove its terminal state)
                # would make recovery and verification non-deterministic.
                if evictable:
                    if collection == "plans":
                        evictable = not any(
                            getattr(item, "plan_id", "") == old_id
                            for item in (
                                *self.actions.values(),
                                *self.observations.values(),
                                *self.outcomes.values(),
                                *self.replans.values(),
                            )
                        )
                    elif collection == "actions":
                        evictable = not any(
                            getattr(item, "action_id", "") == old_id
                            for item in (
                                *self.observations.values(),
                                *self.outcomes.values(),
                            )
                        ) and not any(
                            getattr(step, "last_action_id", "") == old_id
                            for plan in self.plans.values()
                            for step in getattr(plan, "steps", [])
                        )
                    elif collection == "observations":
                        evictable = not any(
                            old_id in getattr(item, "observed_ids", [])
                            for item in self.actions.values()
                        ) and not any(
                            getattr(item, "observation_id", "") == old_id
                            for item in self.outcomes.values()
                        )
                    elif collection == "outcomes":
                        evictable = not any(
                            getattr(item, "outcome_id", "") == old_id
                            for item in self.actions.values()
                        )
                if evictable:
                    candidate = old_id
                    break
            if candidate is None:
                self.stats["capacity_rejections"] += 1
                return False
            order.remove(candidate)
            store.pop(candidate, None)
        store[key] = value
        order.append(key)
        return True

    def _remove(self, collection: str, key: str) -> None:
        getattr(self, collection).pop(key, None)
        try:
            self._order[collection].remove(key)
        except ValueError:
            pass

    def _event(
        self, kind: str, *, plan_id: str = "", step_id: str = "",
        action_id: str = "", tick: int = 0, detail: str = "", data: Any = None,
    ) -> None:
        self.events.append({
            "kind": _redact_text(kind, 80), "plan_id": _text(plan_id, 100),
            "step_id": _text(step_id, 100), "action_id": _text(action_id, 100),
            "tick": _safe_int(tick), "detail": _redact_text(detail, 300),
            "data": _safe_mapping(data), "timestamp": _now_iso(),
        })
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]

    def create_plan(
        self, objective: str, *, goal_id: str = "", source: str = "user",
        steps: Sequence[Any] | None = None, step_specs: Sequence[Any] | None = None,
        max_depth: int | None = None, max_steps: int | None = None,
        max_retries: int | None = None, budget_ticks: int = 0,
        deadline_tick: int = 0, risk_level: str = "low",
        constraints: Mapping[str, Any] | None = None, current_tick: int = 0,
        plan_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> TaskPlan | None:
        objective = _redact_text(objective, 600)
        if not objective:
            self.stats["capacity_rejections"] += 1
            return None
        requested_plan_id = _text(plan_id, 100)
        plan_key = requested_plan_id or _new_id("plan")
        existing_plan = self.plans.get(plan_key)
        if existing_plan is not None:
            # Reusing a caller-supplied plan ID is safe only as an exact
            # idempotent create.  Never replace an existing plan: its action
            # and outcome records may already be the proof for a goal.
            if (
                existing_plan.objective == objective
                and (not goal_id or existing_plan.goal_id == _text(goal_id, 100))
            ):
                return existing_plan
            return None
        depth_limit = self.max_depth if max_depth is None else min(
            self.max_depth, _safe_int(max_depth, self.max_depth, 0, 32)
        )
        step_limit = self.max_steps_per_plan if max_steps is None else min(
            self.max_steps_per_plan, _safe_int(max_steps, self.max_steps_per_plan, 1, 10000)
        )
        plan = TaskPlan(
            id=plan_key, objective=objective,
            goal_id=goal_id, source=source, max_depth=depth_limit, max_steps=step_limit,
            max_retries=self.default_max_retries if max_retries is None else max_retries,
            budget_ticks=budget_ticks, deadline_tick=deadline_tick,
            created_tick=current_tick, updated_tick=current_tick,
            risk_level=risk_level, constraints=constraints or {}, metadata=metadata or {},
        )
        if not self._store("plans", plan.id, plan, self.max_plans):
            return None
        self.stats["plans_created"] += 1
        self._event("plan_created", plan_id=plan.id, tick=current_tick, detail=objective)
        resolved = step_specs if step_specs is not None else steps
        if resolved is not None and not self.decompose_plan(plan.id, resolved, current_tick=current_tick):
            self._remove("plans", plan.id)
            self.stats["plans_created"] = max(0, self.stats["plans_created"] - 1)
            return None
        return plan

    def add_step(
        self, plan_id: str, description: str, *, step_id: str | None = None,
        parent_id: str = "", dependencies: Sequence[str] | str | None = None,
        action_type: str = "", tool_name: str = "", expected: str = "",
        acceptance_criteria: Mapping[str, Any] | str | None = None,
        max_retries: int | None = None, current_tick: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> PlanStep | None:
        plan = self.get_plan(plan_id)
        description = _redact_text(description, 500)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES or not description:
            return None
        if len(plan.steps) >= min(plan.max_steps, self.max_steps_per_plan):
            self.stats["capacity_rejections"] += 1
            return None
        parent = plan.get_step(parent_id) if parent_id else None
        if parent_id and parent is None:
            return None
        depth = parent.depth + 1 if parent else 0
        if depth > min(plan.max_depth, self.max_depth):
            return None
        deps = _dependencies(dependencies or [])
        known = {step.id for step in plan.steps}
        if any(dep not in known for dep in deps):
            return None
        key = _text(step_id, 100) or _new_id("step")
        if key in known:
            return None
        step = PlanStep(
            id=key, plan_id=plan.id, description=description, parent_id=parent_id,
            depth=depth, dependencies=deps, action_type=action_type,
            tool_name=tool_name, expected=expected,
            acceptance_criteria=acceptance_criteria or {},
            max_retries=plan.max_retries if max_retries is None else max_retries,
            created_tick=current_tick, metadata=metadata or {},
        )
        plan.steps.append(step)
        plan.revision += 1
        plan.updated_tick = max(plan.updated_tick, _safe_int(current_tick))
        self.stats["steps_added"] += 1
        self._event("step_added", plan_id=plan.id, step_id=step.id, tick=current_tick, detail=description)
        self._refresh_plan(plan, current_tick)
        return step

    def _flatten_specs(
        self, specs: Sequence[Any], *, parent_id: str = "", depth: int = 0,
        seen: set[int] | None = None,
    ) -> list[dict[str, Any]] | None:
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes, bytearray)):
            return None
        seen = seen if seen is not None else set()
        result: list[dict[str, Any]] = []
        for raw in list(specs):
            if isinstance(raw, str):
                spec: dict[str, Any] = {"description": raw}
            elif isinstance(raw, Mapping):
                marker = id(raw)
                if marker in seen:
                    return None
                seen.add(marker)
                spec = dict(raw)
            else:
                return None
            description = _redact_text(spec.get("description", spec.get("objective", "")), 500)
            declared_depth = _safe_int(spec.get("depth", depth), depth, 0, 100)
            # Nested depth is structural, not caller-controlled. A top-level
            # declaration may request a shallower/deeper starting level only
            # when it remains within the bounded plan depth.
            if depth > 0 and declared_depth != depth:
                return None
            if not description or declared_depth > self.max_depth:
                return None
            key = _text(spec.get("id", ""), 100) or _new_id("step")
            result.append({
                "id": key, "description": description,
                "parent_id": _text(spec.get("parent_id", parent_id), 100) or parent_id,
                "depth": declared_depth,
                # A top-level item may reference a parent declared elsewhere
                # in the same batch and omit ``depth``; its effective depth is
                # then derived from that parent.  Explicit nested declarations
                # are still checked against the structural depth below.
                "depth_explicit": "depth" in spec,
                "dependencies": spec.get("dependencies", spec.get("depends_on", [])),
                "action_type": spec.get("action_type", ""), "tool_name": spec.get("tool_name", ""),
                "expected": spec.get("expected", ""),
                "acceptance_criteria": spec.get("acceptance_criteria", spec.get("criteria", {})),
                "max_retries": spec.get("max_retries"), "created_tick": spec.get("created_tick", 0),
                "metadata": spec.get("metadata", {}),
            })
            children = spec.get("steps", spec.get("children", spec.get("substeps", [])))
            if children:
                nested = self._flatten_specs(children, parent_id=key, depth=depth + 1, seen=seen)
                if nested is None:
                    return None
                result.extend(nested)
            seen.discard(id(raw))
        return result

    def decompose_plan(
        self, plan_id: str, specs: Sequence[Any], *, replace: bool = False,
        current_tick: int = 0,
    ) -> bool:
        plan = self.get_plan(plan_id)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES:
            return False
        flattened = self._flatten_specs(specs)
        if not flattened or len(flattened) > min(plan.max_steps, self.max_steps_per_plan):
            return False
        existing = {step.id for step in plan.steps}
        incoming = [item["id"] for item in flattened]
        if len(set(incoming)) != len(incoming) or (not replace and existing.intersection(incoming)):
            return False
        if replace and any(
            getattr(action, "plan_id", "") == plan.id
            for action in self.actions.values()
        ):
            # Replacing a plan after actions have been journaled would orphan
            # their causal records.  Require a fresh plan (or an explicit
            # operator-level migration) instead of silently rewriting history.
            return False

        known = set(incoming) | (set() if replace else existing)
        if len(known) > min(plan.max_steps, self.max_steps_per_plan):
            return False

        # Validate the complete parent/dependency graph before mutating any
        # object.  Earlier versions only checked cycles among newly supplied
        # dependency edges and trusted list order, which allowed a child to be
        # inserted before its parent or a restored snapshot to deadlock
        # forever.  Effective depth is derived from the parent chain, never
        # from an untrusted nested declaration alone.
        node_specs: dict[str, dict[str, Any]] = {}
        if not replace:
            for old in plan.steps:
                node_specs[old.id] = {
                    "parent_id": old.parent_id,
                    "depth": old.depth,
                    "dependencies": list(old.dependencies),
                    "incoming": False,
                }
        for item in flattened:
            node_specs[item["id"]] = {
                "parent_id": _text(item.get("parent_id", ""), 100),
                "depth": _safe_int(item.get("depth", 0), 0, 0, 100),
                "dependencies": _dependencies(item.get("dependencies", [])),
                "incoming": True,
                "depth_explicit": bool(item.get("depth_explicit", False)),
            }

        for node, spec in node_specs.items():
            parent_id = spec["parent_id"]
            if parent_id and parent_id not in known:
                return False
            if any(dep not in known for dep in spec["dependencies"]):
                return False

        parent_visiting: set[str] = set()
        parent_depths: dict[str, int] = {}

        def _effective_depth(node: str) -> int | None:
            if node in parent_depths:
                return parent_depths[node]
            if node in parent_visiting:
                return None
            spec = node_specs.get(node)
            if spec is None:
                return None
            parent_visiting.add(node)
            parent_id = spec["parent_id"]
            if parent_id:
                parent_depth = _effective_depth(parent_id)
                if parent_depth is None:
                    parent_visiting.discard(node)
                    return None
                depth = parent_depth + 1
                # For an explicitly nested item, the declared depth must agree
                # with the structural depth.  This blocks a child whose
                # declaration tries to hide an out-of-bound grandchild.
                if (
                    spec.get("incoming")
                    and spec.get("depth_explicit")
                    and depth != spec["depth"]
                ):
                    parent_visiting.discard(node)
                    return None
            else:
                depth = _safe_int(spec.get("depth", 0), 0, 0, 100)
            parent_visiting.discard(node)
            if depth > min(plan.max_depth, self.max_depth):
                return None
            parent_depths[node] = depth
            return depth

        if any(_effective_depth(node) is None for node in known):
            return False

        dependency_graph = {
            node: list(node_specs[node]["dependencies"])
            for node in known
        }
        dep_visiting: set[str] = set()
        dep_visited: set[str] = set()

        def _acyclic(node: str) -> bool:
            if node in dep_visiting:
                return False
            if node in dep_visited:
                return True
            dep_visiting.add(node)
            for dep in dependency_graph.get(node, []):
                if not _acyclic(dep):
                    return False
            dep_visiting.discard(node)
            dep_visited.add(node)
            return True

        if any(not _acyclic(node) for node in known):
            return False
        if replace:
            plan.steps = []
        for item in flattened:
            depth = parent_depths.get(item["id"], _safe_int(item["depth"]))
            step = PlanStep(
                id=item["id"], plan_id=plan.id, description=item["description"],
                parent_id=item["parent_id"], depth=depth,
                dependencies=_dependencies(item["dependencies"]),
                action_type=item["action_type"], tool_name=item["tool_name"],
                expected=item["expected"], acceptance_criteria=item["acceptance_criteria"],
                max_retries=plan.max_retries if item["max_retries"] is None else item["max_retries"],
                created_tick=item["created_tick"] or current_tick, metadata=item["metadata"],
            )
            plan.steps.append(step)
            self.stats["steps_added"] += 1
            self._event("step_added", plan_id=plan.id, step_id=step.id, tick=current_tick, detail=step.description)
        plan.revision += 1
        plan.updated_tick = max(plan.updated_tick, _safe_int(current_tick))
        self._refresh_plan(plan, current_tick)
        return True

    decompose = decompose_plan
    create_task_plan = create_plan
    add_plan_step = add_step

    def get_plan(self, plan_id: str = "", goal_id: str = "") -> TaskPlan | None:
        """Return a plan by stable id or its owning Goal id."""
        key = _text(plan_id, 100)
        gid = _text(goal_id, 100)
        if key:
            plan = self.plans.get(key)
            # When both identifiers are supplied they are a single causal
            # constraint, not two independent lookup hints.  Returning a plan
            # for another goal would let a stale/malformed episode cross its
            # execution lane.  Do not silently fall back to a goal lookup on
            # an explicit plan-id miss either: callers that have a stale
            # correlation must fail closed and establish a fresh one.
            if plan is None:
                return None
            if gid and plan.goal_id != gid:
                return None
            return plan
        if gid:
            for plan in self.plans.values():
                if plan.goal_id == gid:
                    return plan
        return None

    def find_action(
        self,
        *,
        action_id: str = "",
        episode_id: str = "",
        plan_id: str = "",
        step_id: str = "",
        include_terminal: bool = True,
    ) -> ActionRecord | None:
        """Find the newest action matching a causal lane."""
        if action_id:
            action = self.actions.get(_text(action_id, 100))
            if action is None:
                return None
            if episode_id and action.episode_id != _text(episode_id, 100):
                return None
            if plan_id and action.plan_id != _text(plan_id, 100):
                return None
            if step_id and action.step_id != _text(step_id, 100):
                return None
            if not include_terminal and action.status in {
                ActionStatus.EVALUATED,
                ActionStatus.CANCELLED,
            }:
                return None
            return action
        matches = []
        for key in reversed(self._order.get("actions", [])):
            action = self.actions.get(key)
            if action is None:
                continue
            if episode_id and action.episode_id != _text(episode_id, 100):
                continue
            if plan_id and action.plan_id != _text(plan_id, 100):
                continue
            if step_id and action.step_id != _text(step_id, 100):
                continue
            if not include_terminal and action.status in {ActionStatus.EVALUATED, ActionStatus.CANCELLED}:
                continue
            matches.append(action)
            break
        return matches[0] if matches else None

    def ensure_plan_for_goal(self, goal: Any, current_tick: int = 0) -> TaskPlan | None:
        """Create a conservative default plan for a legacy Goal.

        Existing callers can still provide an explicit ``step_specs`` plan.
        The default decomposition keeps a single read-only lookup for most
        drives and adds a bounded local-grounding → external-verification
        chain for curiosity/exploration goals.
        """
        if goal is None:
            return None
        goal_id = _text(getattr(goal, "id", ""), 100)
        objective = _redact_text(getattr(goal, "description", ""), 600)
        if not objective:
            return None
        existing = self.get_plan(goal_id=goal_id)
        if existing is not None:
            if not existing.budget_ticks:
                existing.budget_ticks = _safe_int(getattr(goal, "budget_ticks", 0))
            if not existing.deadline_tick:
                existing.deadline_tick = _safe_int(getattr(goal, "deadline_tick", 0))
            existing.updated_tick = max(existing.updated_tick, _safe_int(current_tick))
            return existing
        drive = _text(getattr(goal, "drive", ""), 80).lower()
        metadata = {"drive": drive}
        specs: list[dict[str, Any]] = []
        if drive in {"curiosity", "exploration"}:
            specs = [
                {
                    "id": f"step-{uuid.uuid4().hex[:10]}",
                    "description": f"先检查已有记忆：{objective[:240]}",
                    "action_type": "call_tool", "tool_name": "memory_search",
                    "expected": "获得与目标相关的内部记录",
                    "criteria": {"any_fields": ["memories", "results", "count"]},
                    "metadata": {"query_hint": objective[:240]},
                },
                {
                    "id": f"step-{uuid.uuid4().hex[:10]}",
                    "description": f"再核验外部来源：{objective[:240]}",
                    "action_type": "call_tool", "tool_name": "web_search",
                    "expected": "获得带来源的外部结果",
                    "criteria": {"required_fields": ["results"], "min_count": 1, "provenance_required": True},
                    "dependencies": [],
                    "metadata": {"query_hint": objective[:240]},
                },
            ]
            specs[1]["dependencies"] = [specs[0]["id"]]
        else:
            query = objective
            if drive == "connection":
                query = "最近的对话和未完成的事项"
            elif drive == "self_preservation":
                query = "身份变化 核心认知"
            specs = [{
                "id": f"step-{uuid.uuid4().hex[:10]}",
                "description": f"检索与目标相关的内部记录：{objective[:240]}",
                "action_type": "call_tool", "tool_name": "memory_search",
                "expected": "获得结构化记忆检索结果",
                "criteria": {"any_fields": ["memories", "results", "count"]},
                "metadata": {"query_hint": query[:240]},
            }]
        return self.create_plan(
            objective,
            goal_id=goal_id,
            source=_text(getattr(goal, "task_source", "autonomous"), 100) or "autonomous",
            step_specs=specs,
            current_tick=current_tick,
            budget_ticks=_safe_int(getattr(goal, "budget_ticks", 0)),
            deadline_tick=_safe_int(getattr(goal, "deadline_tick", 0)),
            metadata=metadata,
        )

    def _refresh_plan(self, plan: TaskPlan, current_tick: int = 0) -> None:
        """Repair derived plan state after a mutation or snapshot restore."""
        previous_status = plan.status
        previous_active = plan.active_step_id
        plan.updated_tick = max(plan.updated_tick, _safe_int(current_tick))
        if not plan.steps:
            if plan.status not in TERMINAL_PLAN_STATUSES:
                plan.status = PlanStatus.BLOCKED
            plan.active_step_id = ""
            if plan.status != previous_status or previous_active:
                plan.revision += 1
            return
        by_id = {step.id: step for step in plan.steps}

        # ``COMPLETED`` is a derived claim, never a caller-controlled flag.
        # A hand-edited/partially written snapshot may say a step is complete
        # without retaining the evaluated action and its verified outcome.
        # Quarantine that step before deriving the plan status so a forged
        # snapshot can never project a goal to ``done``.
        for step in plan.steps:
            if step.status != StepStatus.COMPLETED:
                continue
            proven = False
            for action in reversed(self.actions.values()):
                if (
                    action.plan_id == plan.id
                    and action.step_id == step.id
                    and action.status == ActionStatus.EVALUATED
                    and action.outcome_id
                ):
                    outcome = self.outcomes.get(action.outcome_id)
                    if (
                        outcome is not None
                        and outcome.plan_id == plan.id
                        and outcome.step_id == step.id
                        and outcome.status == OutcomeQuality.VERIFIED
                        and outcome.success is True
                    ):
                        proven = True
                        break
            if not proven:
                step.status = StepStatus.PAUSED
                step.result_quality = OutcomeQuality.UNKNOWN
                step.blocked_reason = "完成状态缺少可验证结果记录，已暂停"

        # Validate parent links independently from dependency links.  Parent
        # cycles are another way to create a plan that looks ACTIVE but can
        # never be decomposed into a runnable tree.
        parent_visiting: set[str] = set()
        parent_done: set[str] = set()

        def _parent_acyclic(node: str) -> bool:
            if node in parent_visiting:
                return False
            if node in parent_done:
                return True
            parent_visiting.add(node)
            parent_id = by_id[node].parent_id
            if parent_id and parent_id in by_id and not _parent_acyclic(parent_id):
                return False
            parent_visiting.discard(node)
            parent_done.add(node)
            return True

        if any(not _parent_acyclic(step.id) for step in plan.steps):
            for step in plan.steps:
                if step.status not in TERMINAL_STEP_STATUSES:
                    step.status = StepStatus.BLOCKED
                    step.blocked_reason = "计划父级关系存在循环，已阻止执行"
            plan.active_step_id = ""
            if plan.status not in TERMINAL_PLAN_STATUSES:
                plan.status = PlanStatus.BLOCKED
            if plan.status != previous_status or previous_active:
                plan.revision += 1
            return

        # A malformed/restored graph must become an explicit blocked state,
        # never a silently spinning ACTIVE plan.  Mark only non-terminal
        # affected steps so the audit trail remains intact.
        malformed: list[PlanStep] = []
        for step in plan.steps:
            if step.parent_id and step.parent_id not in by_id:
                malformed.append(step)
                continue
            if any(dep not in by_id for dep in step.dependencies):
                malformed.append(step)
        if malformed:
            for step in malformed:
                if step.status not in TERMINAL_STEP_STATUSES:
                    step.status = StepStatus.BLOCKED
                    step.blocked_reason = "计划依赖引用无效，已阻止执行"
            plan.active_step_id = ""
            if plan.status not in TERMINAL_PLAN_STATUSES:
                plan.status = PlanStatus.BLOCKED
            if plan.status != previous_status or previous_active:
                plan.revision += 1
            return

        running = [step for step in plan.steps if step.status == StepStatus.RUNNING]
        if running:
            # The execution lane is single-flight.  If a hand-edited or old
            # snapshot contains several running steps, retain the first in
            # insertion order and quarantine the extras instead of allowing
            # ambiguous action correlation.
            for extra in running[1:]:
                extra.status = StepStatus.PAUSED
                extra.blocked_reason = "同一计划同时存在多个运行步骤，需显式恢复"
            plan.active_step_id = running[0].id
            if plan.status not in TERMINAL_PLAN_STATUSES:
                plan.status = PlanStatus.ACTIVE
            if plan.status != previous_status or plan.active_step_id != previous_active:
                plan.revision += 1
            return
        plan.active_step_id = ""
        if all(step.status == StepStatus.COMPLETED for step in plan.steps):
            plan.status = PlanStatus.COMPLETED
        elif any(step.status == StepStatus.FAILED for step in plan.steps):
            plan.status = PlanStatus.FAILED
        elif all(step.status == StepStatus.ABANDONED for step in plan.steps):
            plan.status = PlanStatus.ABANDONED
        elif any(step.status in {StepStatus.BLOCKED, StepStatus.PAUSED} for step in plan.steps):
            plan.status = PlanStatus.PAUSED
        else:
            # All remaining steps are pending.  Check whether at least one is
            # dependency-ready.  A cycle or dependency on a terminal
            # non-completed step is a durable BLOCKED condition.
            ready = any(
                step.status == StepStatus.PENDING
                and all(by_id[dep].status == StepStatus.COMPLETED for dep in step.dependencies)
                for step in plan.steps
            )
            if not ready:
                for step in plan.steps:
                    if step.status == StepStatus.PENDING:
                        step.status = StepStatus.BLOCKED
                        step.blocked_reason = "没有可执行的依赖就绪步骤（可能存在循环依赖）"
                plan.status = PlanStatus.BLOCKED
            elif previous_status == PlanStatus.ACTIVE:
                plan.status = PlanStatus.ACTIVE
            elif previous_status in {PlanStatus.PAUSED, PlanStatus.BLOCKED}:
                # Recovery and operator pause deliberately require an explicit
                # resume.  Do not silently reactivate a plan merely because a
                # pending step happens to be dependency-ready.
                plan.status = previous_status
            elif previous_status not in {PlanStatus.PENDING, PlanStatus.ACTIVE}:
                plan.status = PlanStatus.PENDING
        if plan.status != previous_status or plan.active_step_id != previous_active:
            plan.revision += 1

    def _plan_limit_reason(self, plan: TaskPlan, current_tick: int) -> str:
        now = _safe_int(current_tick)
        if plan.budget_ticks > 0 and plan.consumed_ticks >= plan.budget_ticks:
            return "任务计划预算已耗尽"
        if plan.deadline_tick > 0 and now >= plan.deadline_tick:
            return "任务计划截止时间已到"
        return ""

    def next_step(self, plan_id: str = "", goal_id: str = "", *, current_tick: int = 0) -> PlanStep | None:
        """Claim exactly one dependency-ready step at a safe boundary."""
        plan = self.get_plan(plan_id, goal_id)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES or plan.status in {PlanStatus.PAUSED, PlanStatus.BLOCKED}:
            return None
        reason = self._plan_limit_reason(plan, current_tick)
        if reason:
            plan.status = PlanStatus.FAILED
            plan.updated_tick = _safe_int(current_tick)
            self._event("plan_failed", plan_id=plan.id, tick=current_tick, detail=reason)
            return None
        running = next((step for step in plan.steps if step.status == StepStatus.RUNNING), None)
        if running is not None:
            return None
        by_id = {step.id: step for step in plan.steps}
        for step in plan.steps:
            if step.status != StepStatus.PENDING:
                continue
            if any(by_id.get(dep) is None or by_id[dep].status != StepStatus.COMPLETED for dep in step.dependencies):
                continue
            step.status = StepStatus.RUNNING
            step.attempts += 1
            step.started_tick = _safe_int(current_tick)
            plan.status = PlanStatus.ACTIVE
            plan.active_step_id = step.id
            plan.consumed_ticks += 1
            plan.updated_tick = _safe_int(current_tick)
            plan.revision += 1
            self._event("step_claimed", plan_id=plan.id, step_id=step.id, tick=current_tick, detail=f"attempt={step.attempts}")
            return step
        self._refresh_plan(plan, current_tick)
        return None

    claim_step = next_step

    def record_action(
        self, plan_id: str, step_id: str, *, action_type: str = "call_tool",
        tool_name: str = "", args: Mapping[str, Any] | None = None,
        expected: str = "", tick: int = 0, episode_id: str = "",
        attempt_no: int | None = None, action_id: str | None = None,
    ) -> ActionRecord | None:
        plan = self.get_plan(plan_id)
        step = plan.get_step(step_id) if plan else None
        if plan is None or step is None or plan.status in TERMINAL_PLAN_STATUSES:
            return None
        # A paused/blocked plan requires an explicit ``resume_plan`` (or a
        # safe replan) before a caller may create another action.  Also keep
        # the single-flight invariant at the ledger boundary itself; callers
        # should not be able to bypass ``next_step`` and steal the active
        # pointer from a different running step.
        if plan.status in {PlanStatus.PAUSED, PlanStatus.BLOCKED}:
            return None
        running_step = next(
            (candidate for candidate in plan.steps if candidate.status == StepStatus.RUNNING),
            None,
        )
        if running_step is not None and running_step.id != step.id:
            return None
        if (
            step.status == StepStatus.RUNNING
            and plan.active_step_id
            and plan.active_step_id != step.id
        ):
            return None
        # ``record_action`` is also a public ledger boundary for embedders
        # that do not call ``next_step``.  Do not let that shortcut bypass
        # the dependency graph: an action may only be claimed after every
        # referenced predecessor has a completed, verified result (the
        # step's COMPLETED state is itself derived by ``_apply_outcome``).
        by_id = {candidate.id: candidate for candidate in plan.steps}
        if any(
            by_id.get(dependency) is None
            or by_id[dependency].status != StepStatus.COMPLETED
            for dependency in step.dependencies
        ):
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        key = _text(action_id, 100)
        if key:
            existing = self.actions.get(key)
            if existing is not None:
                # An action id is an idempotency key, not a permission to
                # reuse an arbitrary record.  Only an identical immutable
                # request may receive the existing record; a collision with
                # different semantics must fail closed so a stale/replayed
                # caller cannot silently attach a new tool or arguments to an
                # old causal lane.
                requested_action_type = _text(
                    action_type or step.action_type, 80
                )
                requested_tool_name = _text(
                    tool_name or step.tool_name, 120
                )
                requested_args_digest = _digest(args or {})
                requested_expected = _text(expected or step.expected, 300)
                requested_attempt = _safe_int(
                    attempt_no if attempt_no is not None else max(1, step.attempts),
                    1,
                    1,
                )
                requested_episode = _text(episode_id, 100)
                if (
                    existing.plan_id != plan.id
                    or existing.step_id != step.id
                    or existing.action_type != requested_action_type
                    or existing.tool_name != requested_tool_name
                    or existing.args_digest != requested_args_digest
                    or existing.expected != requested_expected
                    or existing.attempt_no != requested_attempt
                    or existing.episode_id != requested_episode
                ):
                    return None
                return existing
        # One step has one in-flight action.  Without this guard a caller
        # could append several PLANNED records to the same RUNNING step and
        # later start whichever callback arrived first, creating an ambiguous
        # external side-effect history.  Retries are represented by a new
        # action only after the previous action has been evaluated/cancelled.
        if any(
            candidate.plan_id == plan.id
            and candidate.step_id == step.id
            and candidate.status in {
                ActionStatus.PLANNED,
                ActionStatus.STARTED,
                ActionStatus.OBSERVED,
            }
            and not candidate.outcome_id
            for candidate in self.actions.values()
        ):
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        plan_before = {
            "status": plan.status,
            "active_step_id": plan.active_step_id,
            "updated_tick": plan.updated_tick,
            "consumed_ticks": plan.consumed_ticks,
            "revision": plan.revision,
        }
        step_before = {
            "status": step.status,
            "attempts": step.attempts,
            "started_tick": step.started_tick,
            "last_action_id": step.last_action_id,
        }
        claimed_here = False
        if step.status == StepStatus.PENDING:
            # Direct embedders may create an action without calling
            # ``next_step`` first.  Treat that as the same single-step claim,
            # including attempt/budget accounting, and roll it back below if
            # the action journal cannot accept the record.
            if self._plan_limit_reason(plan, tick):
                return None
            step.status = StepStatus.RUNNING
            step.attempts += 1
            step.started_tick = _safe_int(tick)
            plan.status = PlanStatus.ACTIVE
            plan.active_step_id = step.id
            plan.consumed_ticks += 1
            plan.updated_tick = _safe_int(tick)
            plan.revision += 1
            claimed_here = True
        if step.status != StepStatus.RUNNING:
            return None
        action = ActionRecord(
            id=key or _new_id("action"), plan_id=plan.id,
            step_id=step.id, action_type=action_type or step.action_type,
            tool_name=tool_name or step.tool_name, args_digest=_digest(args or {}),
            args_summary=_redact_text(self._args_summary(args or {}), 600),
            expected=expected or step.expected, tick=tick,
            attempt_no=attempt_no if attempt_no is not None else max(1, step.attempts),
            episode_id=episode_id,
        )
        if action.id in self.actions:
            if claimed_here:
                plan.status = plan_before["status"]
                plan.active_step_id = plan_before["active_step_id"]
                plan.updated_tick = plan_before["updated_tick"]
                plan.consumed_ticks = plan_before["consumed_ticks"]
                plan.revision = plan_before["revision"]
                step.status = step_before["status"]
                step.attempts = step_before["attempts"]
                step.started_tick = step_before["started_tick"]
                step.last_action_id = step_before["last_action_id"]
            return None
        if not self._store("actions", action.id, action, self.max_actions):
            # Do not strand a RUNNING claim when the bounded action journal is
            # full.  The higher-level BrainStem hand-off also keeps a
            # before-image, but the standalone ledger must be safe on its own.
            if claimed_here:
                plan.status = plan_before["status"]
                plan.active_step_id = plan_before["active_step_id"]
                plan.updated_tick = plan_before["updated_tick"]
                plan.consumed_ticks = plan_before["consumed_ticks"]
                plan.revision = plan_before["revision"]
                step.status = step_before["status"]
                step.attempts = step_before["attempts"]
                step.started_tick = step_before["started_tick"]
                step.last_action_id = step_before["last_action_id"]
            elif not step.last_action_id:
                # ``next_step`` may have been called by a standalone caller
                # immediately before this method.  There is no action to
                # service that RUNNING claim, so quarantine it explicitly
                # instead of leaving a permanently busy plan behind.
                step.status = StepStatus.PAUSED
                step.blocked_reason = "行动账本容量不足，需显式恢复"
                if plan.active_step_id == step.id:
                    plan.active_step_id = ""
                plan.status = PlanStatus.PAUSED
                plan.updated_tick = _safe_int(tick)
                plan.revision += 1
                self._event(
                    "plan_paused",
                    plan_id=plan.id,
                    step_id=step.id,
                    tick=tick,
                    detail=step.blocked_reason,
                )
            return None
        step.last_action_id = action.id
        if claimed_here:
            self._event(
                "step_claimed",
                plan_id=plan.id,
                step_id=step.id,
                action_id=action.id,
                tick=tick,
                detail=f"attempt={step.attempts}",
            )
        self.stats["actions_recorded"] += 1
        self._event("action_recorded", plan_id=plan.id, step_id=step.id, action_id=action.id, tick=tick, detail=action.tool_name)
        return action

    def start_action(self, action_id: str, *, tick: int = 0) -> bool:
        action = self.actions.get(_text(action_id, 100))
        if action is None or action.status != ActionStatus.PLANNED:
            return False
        plan = self.plans.get(action.plan_id)
        step = plan.get_step(action.step_id) if plan else None
        if (
            plan is None
            or step is None
            or plan.status in TERMINAL_PLAN_STATUSES
            or step.status != StepStatus.RUNNING
            or (plan.active_step_id and plan.active_step_id != step.id)
            or (
                step.last_action_id
                and step.last_action_id != action.id
            )
        ):
            return False
        action.status = ActionStatus.STARTED
        action.started_tick = _safe_int(tick)
        self._event("action_started", plan_id=action.plan_id, step_id=action.step_id, action_id=action.id, tick=tick)
        return True

    def _make_observation(self, action: ActionRecord, value: Any, tick: int) -> ObservationRecord:
        if isinstance(value, ObservationRecord):
            value.action_id = action.id
            value.tick = _safe_int(tick)
            return value
        if isinstance(value, Mapping):
            raw_data = value.get("data", value.get("payload", {}))
            if not isinstance(raw_data, Mapping):
                raw_data = {
                    k: v for k, v in value.items()
                    if k not in {"id", "summary", "text", "result", "provenance", "simulated", "error"}
                }
            return ObservationRecord(
                id=value.get("id", "") or _new_id("observation"), action_id=action.id,
                summary=value.get("summary", value.get("text", value.get("result", ""))),
                data=raw_data, provenance=value.get("provenance", {}),
                tick=tick, simulated=value.get("simulated", False), error=value.get("error", ""),
            )
        return ObservationRecord(id=_new_id("observation"), action_id=action.id, summary=value, tick=tick)

    def record_observation(
        self, action_id: str, observation: Any, *, tick: int = 0,
        evaluate: bool = True, explicit_success: bool | None = None,
        explicit_quality: str | None = None,
    ) -> ObservationRecord | OutcomeRecord | None:
        action = self.actions.get(_text(action_id, 100))
        if action is None or action.status in {ActionStatus.EVALUATED, ActionStatus.CANCELLED}:
            return None
        # An observation is evidence about an action that actually crossed
        # the dispatch boundary.  Accepting it for a merely PLANNED action
        # would let an unstarted/stale callback manufacture a successful
        # result and would make restart recovery unable to distinguish an
        # external side effect from an in-memory intention.
        if action.status not in {ActionStatus.STARTED, ActionStatus.OBSERVED}:
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        plan = self.plans.get(action.plan_id)
        step = plan.get_step(action.step_id) if plan is not None else None
        if (
            plan is None
            or step is None
            or plan.status in TERMINAL_PLAN_STATUSES
            or step.status != StepStatus.RUNNING
            or (plan.active_step_id and plan.active_step_id != step.id)
            or (step.last_action_id and step.last_action_id != action.id)
        ):
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        record = self._make_observation(action, observation, tick)
        existing = self.observations.get(record.id)
        if existing is not None:
            # Observation IDs are immutable evidence keys.  Re-delivery of
            # the exact same record is idempotent, but a caller must not
            # overwrite evidence belonging to another action (or replace the
            # original payload under a reused ID).
            same_evidence = (
                existing.action_id == action.id
                and existing.summary == record.summary
                and existing.data == record.data
                and existing.provenance.to_dict() == record.provenance.to_dict()
                and existing.simulated == record.simulated
                and existing.error == record.error
            )
            if not same_evidence:
                return None
            if evaluate:
                return self.evaluate_action(
                    action.id,
                    tick=tick,
                    explicit_success=explicit_success,
                    explicit_quality=explicit_quality,
                )
            return existing
        if not self._store("observations", record.id, record, self.max_observations):
            self._pause_for_capacity(action, "观察账本容量不足", tick)
            return None
        action.status = ActionStatus.OBSERVED
        action.observed_ids.append(record.id)
        self.stats["observations_recorded"] += 1
        self._event("observation_recorded", plan_id=action.plan_id, step_id=action.step_id, action_id=action.id, tick=tick, detail=record.summary)
        if not evaluate:
            return record
        return self.evaluate_action(action.id, tick=tick, explicit_success=explicit_success, explicit_quality=explicit_quality)

    def evaluate_action(
        self, action_id: str, *, tick: int = 0,
        explicit_success: bool | None = None, explicit_quality: str | None = None,
    ) -> OutcomeRecord | None:
        action = self.actions.get(_text(action_id, 100))
        if action is None or action.status == ActionStatus.EVALUATED:
            if action is not None and action.status == ActionStatus.EVALUATED:
                return self.outcomes.get(action.outcome_id)
            return None
        if action.status != ActionStatus.OBSERVED:
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        observation = next((self.observations.get(item) for item in reversed(action.observed_ids) if item in self.observations), None)
        if observation is None:
            return None
        plan = self.plans.get(action.plan_id)
        step = plan.get_step(action.step_id) if plan else None
        if (
            plan is None
            or step is None
            or plan.status in TERMINAL_PLAN_STATUSES
            or step.status != StepStatus.RUNNING
            or (plan.active_step_id and plan.active_step_id != step.id)
            or (step.last_action_id and step.last_action_id != action.id)
        ):
            self.stats["lifecycle_rejections"] = self.stats.get(
                "lifecycle_rejections", 0
            ) + 1
            return None
        try:
            evaluation = self.evaluator.evaluate(
                observation, step.acceptance_criteria if step else {},
                explicit_success=explicit_success, explicit_quality=explicit_quality,
            )
        except Exception as exc:
            evaluation = EvaluationResult(OutcomeQuality.UNKNOWN, None, f"成果评估异常：{type(exc).__name__}", 0.0, {})
        outcome = OutcomeRecord(
            id=_new_id("outcome"), action_id=action.id, plan_id=action.plan_id,
            step_id=action.step_id, status=evaluation.status, success=evaluation.success,
            summary=observation.summary, reason=evaluation.reason,
            confidence=evaluation.confidence, evaluator=evaluation.evaluator,
            observation_id=observation.id, tick=tick, checks=evaluation.checks,
        )
        if not self._store("outcomes", outcome.id, outcome, self.max_outcomes):
            self._pause_for_capacity(action, "结果账本容量不足", tick)
            return None
        action.status = ActionStatus.EVALUATED
        action.outcome_id = outcome.id
        self.stats["outcomes_recorded"] += 1
        self.stats[f"{outcome.status}_outcomes"] = self.stats.get(f"{outcome.status}_outcomes", 0) + 1
        self._apply_outcome(plan, step, outcome, tick)
        return outcome

    def _pause_for_capacity(
        self, action: ActionRecord | None, reason: str, tick: int = 0
    ) -> None:
        """Stop a lane safely when a bounded journal cannot accept a record."""
        if action is None:
            return
        # Capacity exhaustion is an ambiguous external boundary.  Mark the
        # action terminal before pausing the plan so a later resume/restart
        # cannot replay it or mistake a partially written record for a live
        # dispatch.  The retained observation (if any) remains immutable and
        # can be inspected during an explicit recovery/replan.
        if action.status not in {ActionStatus.EVALUATED, ActionStatus.CANCELLED}:
            action.status = ActionStatus.CANCELLED
            action.started_tick = None
            self._event(
                "action_cancelled",
                plan_id=action.plan_id,
                step_id=action.step_id,
                action_id=action.id,
                tick=tick,
                detail=reason,
            )
        plan = self.plans.get(action.plan_id)
        step = plan.get_step(action.step_id) if plan else None
        if step is not None and step.status not in TERMINAL_STEP_STATUSES:
            step.status = StepStatus.PAUSED
            step.blocked_reason = _redact_text(reason, 240)
        if plan is not None and plan.status not in TERMINAL_PLAN_STATUSES:
            plan.active_step_id = ""
            plan.status = PlanStatus.PAUSED
            plan.updated_tick = _safe_int(tick)
            plan.revision += 1
            self._event(
                "plan_paused",
                plan_id=plan.id,
                step_id=action.step_id,
                action_id=action.id,
                tick=tick,
                detail=reason,
            )

    def cancel_action(
        self,
        action_id: str,
        reason: str = "行动已取消",
        *,
        tick: int = 0,
        pause_step: bool = True,
    ) -> bool:
        """Cancel one unconfirmed action and quarantine its step.

        Cancellation is intentionally terminal for the action.  It is used on
        abort, timeout and restart boundaries; replaying the same external
        side effect after an ambiguous crash is less safe than requiring an
        explicit resume/retry decision.
        """
        action = self.actions.get(_text(action_id, 100))
        if action is None or action.status in {
            ActionStatus.CANCELLED,
            ActionStatus.EVALUATED,
        }:
            return False
        action.status = ActionStatus.CANCELLED
        action.started_tick = None
        plan = self.plans.get(action.plan_id)
        step = plan.get_step(action.step_id) if plan else None
        if pause_step and step is not None and step.status not in TERMINAL_STEP_STATUSES:
            step.status = StepStatus.PAUSED
            step.blocked_reason = _redact_text(reason, 240)
        if plan is not None and plan.status not in TERMINAL_PLAN_STATUSES:
            if plan.active_step_id == action.step_id:
                plan.active_step_id = ""
            if pause_step:
                plan.status = PlanStatus.PAUSED
            plan.updated_tick = _safe_int(tick)
            plan.revision += 1
        self._event(
            "action_cancelled",
            plan_id=action.plan_id,
            step_id=action.step_id,
            action_id=action.id,
            tick=tick,
            detail=reason,
        )
        return True

    cancel = cancel_action

    def cancel_for_episode(
        self,
        episode_id: str,
        reason: str = "自主经历已中止",
        *,
        plan_id: str = "",
        action_id: str = "",
        step_id: str = "",
        tick: int = 0,
    ) -> int:
        """Cancel every still-unconfirmed action in one causal episode.

        The optional IDs narrow the match when an episode contains multiple
        sequential steps.  A missing/unknown ID is not allowed to affect an
        unrelated plan.
        """
        episode_id = _text(episode_id, 100)
        plan_id = _text(plan_id, 100)
        action_id = _text(action_id, 100)
        step_id = _text(step_id, 100)
        # An empty episode identifier is never a valid causal scope.  Without
        # this fail-closed guard a direct caller could accidentally cancel all
        # unconfirmed actions by passing ``""`` with no narrower identifier.
        if not episode_id:
            return 0
        candidates = []
        for action in list(self.actions.values()):
            if action.status in {ActionStatus.CANCELLED, ActionStatus.EVALUATED}:
                continue
            if episode_id and action.episode_id != episode_id:
                continue
            if plan_id and action.plan_id != plan_id:
                continue
            if action_id and action.id != action_id:
                continue
            if step_id and action.step_id != step_id:
                continue
            candidates.append(action)
        cancelled = 0
        touched_plans: set[str] = set()
        for action in candidates:
            if self.cancel_action(action.id, reason, tick=tick, pause_step=True):
                cancelled += 1
                touched_plans.add(action.plan_id)
        # If the action was already evaluated, do not downgrade its verified
        # outcome.  Still clear an orphaned active pointer if the caller named
        # a plan and no unconfirmed action remains.
        for pid in touched_plans:
            plan = self.plans.get(pid)
            if plan is not None:
                plan.active_step_id = ""
                if plan.status not in TERMINAL_PLAN_STATUSES:
                    plan.status = PlanStatus.PAUSED
        return cancelled

    record_action_result = record_observation

    # Small compatibility surface for embedders that use noun-oriented names
    # rather than the explicit action/observation terminology.
    record_result = record_observation
    evaluate_result = evaluate_action

    def _apply_outcome(self, plan: TaskPlan | None, step: PlanStep | None, outcome: OutcomeRecord, tick: int) -> None:
        if plan is None or step is None:
            return
        if outcome.status == OutcomeQuality.VERIFIED and outcome.success is True:
            step.status = StepStatus.COMPLETED
            step.result_quality = OutcomeQuality.VERIFIED
            step.result_summary = outcome.summary
            step.completed_tick = _safe_int(tick)
            step.blocked_reason = ""
            plan.active_step_id = ""
            plan.updated_tick = _safe_int(tick)
            self._event("step_verified", plan_id=plan.id, step_id=step.id, tick=tick, detail=outcome.id)
            self._refresh_plan(plan, tick)
            return
        if outcome.status in {OutcomeQuality.UNKNOWN, OutcomeQuality.SIMULATED} and outcome.success is True:
            self.stats["false_success_prevented"] = self.stats.get("false_success_prevented", 0) + 1
        step.result_quality = outcome.status
        step.result_summary = outcome.reason or outcome.summary
        step.retries += 1
        plan.active_step_id = ""
        plan.updated_tick = _safe_int(tick)
        if step.can_retry:
            step.status = StepStatus.PENDING
            plan.status = PlanStatus.ACTIVE
            self.stats["retries_requested"] += 1
            self._event("step_retry", plan_id=plan.id, step_id=step.id, tick=tick, detail=outcome.status)
            return
        if outcome.status in {OutcomeQuality.UNKNOWN, OutcomeQuality.SIMULATED}:
            if self.auto_replan and step.replan_count < 2:
                proposal = self.propose_replan(
                    plan.id,
                    step.id,
                    outcome.reason or "未获得足够证据",
                    tick=tick,
                )
                if proposal is not None and proposal.safe and self.apply_replan(
                    proposal.id, tick=tick
                ):
                    return
            step.status = StepStatus.BLOCKED
            step.blocked_reason = "结果未核验，已暂停等待更可靠证据"
            plan.status = PlanStatus.PAUSED
            self._event("plan_paused", plan_id=plan.id, step_id=step.id, tick=tick, detail=step.blocked_reason)
        else:
            if self.auto_replan and step.replan_count < 2:
                proposal = self.propose_replan(
                    plan.id,
                    step.id,
                    outcome.reason or "行动失败",
                    tick=tick,
                )
                if proposal is not None and proposal.safe and self.apply_replan(
                    proposal.id, tick=tick
                ):
                    return
            step.status = StepStatus.FAILED
            plan.status = PlanStatus.FAILED
            self._event("plan_failed", plan_id=plan.id, step_id=step.id, tick=tick, detail=outcome.reason)

    def propose_replan(
        self, plan_id: str, step_id: str, reason: str, *,
        alternative: Mapping[str, Any] | None = None, tick: int = 0,
        apply: bool = False,
    ) -> ReplanProposal | None:
        plan = self.get_plan(plan_id)
        step = plan.get_step(step_id) if plan else None
        if (
            plan is None
            or step is None
            or plan.status in TERMINAL_PLAN_STATUSES
            or step.status in TERMINAL_STEP_STATUSES
        ):
            return None
        alt = dict(alternative or {})
        if not alt:
            if step.tool_name == "web_search":
                alt = {"tool_name": "memory_search", "action_type": "call_tool", "acceptance_criteria": {"any_fields": ["memories", "results", "count"]}}
            elif step.tool_name == "memory_search":
                alt = {"tool_name": "web_search", "action_type": "call_tool", "acceptance_criteria": {"required_fields": ["results"], "min_count": 1, "provenance_required": True}}
        tool = _text(alt.get("tool_name", ""), 120)
        safe = tool in {"memory_search", "web_search", "file_read"}
        proposal = ReplanProposal(
            id=_new_id("replan"), plan_id=plan.id, step_id=step.id,
            reason=reason, alternative=alt, safe=safe,
            requires_approval=not safe, tick=tick,
        )
        if not self._store("replans", proposal.id, proposal, self.max_replans):
            return None
        self.stats["replans_proposed"] += 1
        self._event("replan_proposed", plan_id=plan.id, step_id=step.id, tick=tick, detail=reason)
        if apply and safe:
            self.apply_replan(proposal.id, tick=tick)
        return proposal

    def apply_replan(self, proposal_id: str, *, tick: int = 0, approved: bool = False) -> bool:
        proposal = self.replans.get(_text(proposal_id, 100))
        plan = self.plans.get(proposal.plan_id) if proposal else None
        step = plan.get_step(proposal.step_id) if plan else None
        if (
            proposal is None
            or plan is None
            or step is None
            or proposal.status not in {"proposed", "approved"}
            or plan.status in TERMINAL_PLAN_STATUSES
            or step.status in TERMINAL_STEP_STATUSES
        ):
            return False
        # A replan changes the future action definition.  It must never race
        # a PLANNED/STARTED/OBSERVED action whose external side effect may
        # already be underway; pause/cancel that lane first and then request
        # a fresh proposal.  Bound manual and automatic replans alike.
        if any(
            action.plan_id == plan.id
            and action.step_id == step.id
            and action.status in {
                ActionStatus.PLANNED,
                ActionStatus.STARTED,
                ActionStatus.OBSERVED,
            }
            and not action.outcome_id
            for action in self.actions.values()
        ):
            return False
        if step.replan_count >= 2:
            proposal.status = "expired"
            return False
        if proposal.requires_approval and not (approved or proposal.approved):
            proposal.status = "requires_operator"
            return False
        tool = _text(proposal.alternative.get("tool_name", ""), 120)
        if tool and tool not in self.SAFE_READ_TOOLS and not (approved or proposal.approved):
            return False
        if tool:
            step.tool_name = tool
        if proposal.alternative.get("action_type"):
            step.action_type = _text(proposal.alternative.get("action_type"), 80)
        if isinstance(proposal.alternative.get("acceptance_criteria"), Mapping):
            step.acceptance_criteria = _criteria(proposal.alternative["acceptance_criteria"])
        step.replan_count += 1
        step.status = StepStatus.PENDING
        step.blocked_reason = ""
        plan.status = PlanStatus.ACTIVE
        proposal.approved = bool(approved or proposal.approved)
        proposal.status = "applied"
        plan.revision += 1
        plan.updated_tick = _safe_int(tick)
        self.stats["replans_applied"] += 1
        self._event("replan_applied", plan_id=plan.id, step_id=step.id, tick=tick, detail=tool)
        return True

    def pause_plan(self, plan_id: str, reason: str = "操作者暂停", *, tick: int = 0) -> bool:
        plan = self.get_plan(plan_id)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES:
            return False
        safe_reason = _redact_text(reason, 240)
        touched_steps: set[str] = set()
        for action in list(self.actions.values()):
            if (
                action.plan_id == plan.id
                and action.status in {
                    ActionStatus.PLANNED,
                    ActionStatus.STARTED,
                    ActionStatus.OBSERVED,
                }
                and not action.outcome_id
            ):
                action.status = ActionStatus.CANCELLED
                action.started_tick = None
                touched_steps.add(action.step_id)
                self._event(
                    "action_cancelled",
                    plan_id=plan.id,
                    step_id=action.step_id,
                    action_id=action.id,
                    tick=tick,
                    detail=safe_reason,
                )
        step = plan.get_step(plan.active_step_id) if plan.active_step_id else None
        if step is not None:
            touched_steps.add(step.id)
        for step_id in touched_steps:
            candidate = plan.get_step(step_id)
            if candidate is not None and candidate.status not in TERMINAL_STEP_STATUSES:
                candidate.status = StepStatus.PAUSED
                candidate.blocked_reason = safe_reason
        plan.active_step_id = ""
        plan.status = PlanStatus.PAUSED
        plan.updated_tick = _safe_int(tick)
        plan.revision += 1
        self._event("plan_paused", plan_id=plan.id, tick=tick, detail=reason)
        return True

    def resume_plan(self, plan_id: str, *, tick: int = 0) -> bool:
        plan = self.get_plan(plan_id)
        if plan is None or plan.status not in {PlanStatus.PAUSED, PlanStatus.BLOCKED}:
            return False
        for step in plan.steps:
            if step.status in {StepStatus.PAUSED, StepStatus.BLOCKED}:
                step.status = StepStatus.PENDING
                step.blocked_reason = ""
        plan.status = PlanStatus.ACTIVE
        plan.updated_tick = _safe_int(tick)
        plan.revision += 1
        self._event("plan_resumed", plan_id=plan.id, tick=tick)
        return True

    def abandon_plan(self, plan_id: str, reason: str = "操作者放弃", *, tick: int = 0) -> bool:
        plan = self.get_plan(plan_id)
        if plan is None or plan.status in TERMINAL_PLAN_STATUSES:
            return False
        plan.status = PlanStatus.ABANDONED
        plan.active_step_id = ""
        for action in self.actions.values():
            if (
                action.plan_id == plan.id
                and action.status in {
                    ActionStatus.PLANNED,
                    ActionStatus.STARTED,
                    ActionStatus.OBSERVED,
                }
                and not action.outcome_id
            ):
                action.status = ActionStatus.CANCELLED
                action.started_tick = None
                self._event(
                    "action_cancelled",
                    plan_id=plan.id,
                    step_id=action.step_id,
                    action_id=action.id,
                    tick=tick,
                    detail=reason,
                )
        for step in plan.steps:
            if step.status not in TERMINAL_STEP_STATUSES:
                step.status = StepStatus.ABANDONED
                step.blocked_reason = _redact_text(reason, 240)
        plan.revision += 1
        plan.updated_tick = _safe_int(tick)
        self._event("plan_abandoned", plan_id=plan.id, tick=tick, detail=reason)
        return True

    pause = pause_plan
    resume = resume_plan
    abandon = abandon_plan

    def get_outcome(self, outcome_id: str = "") -> OutcomeRecord | None:
        return self.outcomes.get(_text(outcome_id, 100))

    def get_task(self, plan_id: str = "", goal_id: str = "") -> TaskPlan | None:
        return self.get_plan(plan_id=plan_id, goal_id=goal_id)

    def _reconcile_loaded_state(self, *, current_tick: int = 0) -> None:
        """Reconcile cross-record references after loading a snapshot.

        A power loss can occur between any two journal writes.  In particular,
        an evaluated action may be durable while the step transition is not,
        or a running step may be durable before its action record exists.  The
        recovery rule is conservative: preserve a durable outcome, but cancel
        every ambiguous action and pause any step that has no authoritative
        in-flight action.  No tool is replayed here.
        """
        touched_plans: set[str] = set()

        for action in list(self.actions.values()):
            plan = self.plans.get(action.plan_id)
            step = plan.get_step(action.step_id) if plan else None
            if plan is None or step is None:
                if action.status not in {ActionStatus.EVALUATED, ActionStatus.CANCELLED}:
                    action.status = ActionStatus.CANCELLED
                    action.started_tick = None
                continue

            if action.outcome_id:
                outcome = self.outcomes.get(action.outcome_id)
                if outcome is None:
                    # A dangling outcome reference cannot establish success.
                    if action.status != ActionStatus.CANCELLED:
                        action.status = ActionStatus.CANCELLED
                        action.started_tick = None
                    if step.status not in TERMINAL_STEP_STATUSES:
                        step.status = StepStatus.PAUSED
                        step.blocked_reason = "恢复时发现缺失的结果记录"
                    plan.status = PlanStatus.PAUSED
                    plan.active_step_id = ""
                    touched_plans.add(plan.id)
                    continue
                # Outcome and action are the durable source of truth.  Repair
                # a torn step transition without re-running retry/replan side
                # effects or incrementing aggregate counters a second time.
                action.status = ActionStatus.EVALUATED
                if outcome.status == OutcomeQuality.VERIFIED and outcome.success is True:
                    step.status = StepStatus.COMPLETED
                    step.result_quality = OutcomeQuality.VERIFIED
                    step.result_summary = outcome.summary
                    step.completed_tick = outcome.tick
                    step.blocked_reason = ""
                    if plan.active_step_id == step.id:
                        plan.active_step_id = ""
                elif outcome.status in {OutcomeQuality.UNKNOWN, OutcomeQuality.SIMULATED}:
                    if step.status == StepStatus.RUNNING:
                        step.status = StepStatus.PAUSED
                        step.blocked_reason = "恢复时结果未核验，需显式恢复"
                    plan.status = PlanStatus.PAUSED
                    plan.active_step_id = ""
                    touched_plans.add(plan.id)
                elif outcome.status == OutcomeQuality.FAILED:
                    if step.status == StepStatus.RUNNING:
                        step.status = (
                            StepStatus.PENDING if step.can_retry else StepStatus.FAILED
                        )
                    if step.status == StepStatus.FAILED:
                        plan.status = PlanStatus.FAILED
                    else:
                        plan.status = PlanStatus.ACTIVE
                    plan.active_step_id = ""
                continue

            if action.status in {
                ActionStatus.PLANNED,
                ActionStatus.STARTED,
                ActionStatus.OBSERVED,
            }:
                action.status = ActionStatus.CANCELLED
                action.started_tick = None
                if step.status not in TERMINAL_STEP_STATUSES:
                    step.status = StepStatus.PAUSED
                    step.blocked_reason = "重启后未确认的行动已取消，需显式恢复"
                if plan.status not in TERMINAL_PLAN_STATUSES:
                    plan.status = PlanStatus.PAUSED
                    plan.active_step_id = ""
                touched_plans.add(plan.id)

        # A claim can be persisted before record_action.  Scan steps as well
        # as actions so that window is recoverable and never remains RUNNING.
        for plan in self.plans.values():
            for step in plan.steps:
                if step.status != StepStatus.RUNNING:
                    continue
                active_action = any(
                    action.plan_id == plan.id
                    and action.step_id == step.id
                    and action.status in {
                        ActionStatus.PLANNED,
                        ActionStatus.STARTED,
                        ActionStatus.OBSERVED,
                    }
                    and not action.outcome_id
                    for action in self.actions.values()
                )
                if not active_action:
                    step.status = StepStatus.PAUSED
                    step.blocked_reason = "恢复时未找到对应行动，需显式恢复"
                    plan.status = PlanStatus.PAUSED
                    plan.active_step_id = ""
                    touched_plans.add(plan.id)

            if plan.id in touched_plans:
                plan.updated_tick = max(plan.updated_tick, _safe_int(current_tick))
                plan.revision += 1
            self._refresh_plan(plan, current_tick)

    def recover_inflight(self, *, current_tick: int = 0) -> int:
        """Cancel unconfirmed actions after a crash; never replay them."""
        before = {
            action.id
            for action in self.actions.values()
            if action.status in {
                ActionStatus.PLANNED,
                ActionStatus.STARTED,
                ActionStatus.OBSERVED,
            }
            and not action.outcome_id
        }
        self._reconcile_loaded_state(current_tick=current_tick)
        recovered_ids = [
            action.id
            for action in self.actions.values()
            if action.id in before and action.status == ActionStatus.CANCELLED
        ]
        for action_id in recovered_ids:
            action = self.actions.get(action_id)
            if action is not None:
                self._event(
                    "inflight_recovered",
                    plan_id=action.plan_id,
                    step_id=action.step_id,
                    action_id=action.id,
                    tick=current_tick,
                )
        if recovered_ids:
            self.stats["recovered_actions"] = self.stats.get("recovered_actions", 0) + len(recovered_ids)
        return len(recovered_ids)

    def metrics(self) -> dict[str, Any]:
        completed = sum(1 for plan in self.plans.values() if plan.status == PlanStatus.COMPLETED)
        terminal = sum(1 for plan in self.plans.values() if plan.status in TERMINAL_PLAN_STATUSES)
        return {
            **dict(self.stats),
            "plans": len(self.plans), "steps": sum(len(p.steps) for p in self.plans.values()),
            "actions": len(self.actions), "observations": len(self.observations),
            "outcomes": len(self.outcomes), "completed_plans": completed,
            "terminal_plans": terminal,
            "completion_rate": round(completed / max(1, terminal), 4),
            "recovered_actions": self.stats.get("recovered_actions", 0),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "version": 1, "metrics": self.metrics(),
            "plans": [
                {
                    "id": p.id, "goal_id": p.goal_id, "objective": p.objective[:180],
                    "status": p.status, "active_step_id": p.active_step_id,
                    "steps": len(p.steps),
                    "completed_steps": sum(1 for s in p.steps if s.status == StepStatus.COMPLETED),
                    "revision": p.revision,
                }
                for p in list(self.plans.values())[-self.max_plans:]
            ],
        }

    def detail(self, plan_id: str = "", goal_id: str = "") -> dict[str, Any] | None:
        plan = self.get_plan(plan_id, goal_id)
        if plan is None:
            return None
        result = plan.to_dict()
        result["actions"] = [a.to_dict() for a in self.actions.values() if a.plan_id == plan.id][-64:]
        action_ids = {item.get("id") for item in result["actions"] if isinstance(item, Mapping)}
        result["observations"] = [
            observation.to_dict()
            for observation in self.observations.values()
            if observation.action_id in action_ids
        ][-64:]
        result["outcomes"] = [o.to_dict() for o in self.outcomes.values() if o.plan_id == plan.id][-64:]
        result["replans"] = [r.to_dict() for r in self.replans.values() if r.plan_id == plan.id][-32:]
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "limits": {
                "max_plans": self.max_plans, "max_steps_per_plan": self.max_steps_per_plan,
                "max_actions": self.max_actions, "max_observations": self.max_observations,
                "max_outcomes": self.max_outcomes, "max_replans": self.max_replans,
                "max_events": self.max_events, "max_depth": self.max_depth,
                "default_max_retries": self.default_max_retries,
                "require_verified_completion": bool(self.require_verified_completion),
                "auto_replan": bool(self.auto_replan),
            },
            "plans": [self.plans[key].to_dict() for key in self._order["plans"] if key in self.plans][-self.max_plans:],
            "actions": [self.actions[key].to_dict() for key in self._order["actions"] if key in self.actions][-self.max_actions:],
            "observations": [self.observations[key].to_dict() for key in self._order["observations"] if key in self.observations][-self.max_observations:],
            "outcomes": [self.outcomes[key].to_dict() for key in self._order["outcomes"] if key in self.outcomes][-self.max_outcomes:],
            "replans": [self.replans[key].to_dict() for key in self._order["replans"] if key in self.replans][-self.max_replans:],
            "events": [_safe_mapping(item) for item in self.events[-self.max_events:]],
            "stats": dict(self.stats),
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> "TaskExecutionLedger":
        if not isinstance(data, Mapping):
            return cls()
        limits = data.get("limits") if isinstance(data.get("limits"), Mapping) else {}
        def lim(name: str, default: int) -> int:
            return _safe_int(limits.get(name, default), default, 1, 100000)
        ledger = cls(
            max_plans=lim("max_plans", DEFAULT_MAX_PLANS),
            max_steps_per_plan=lim("max_steps_per_plan", DEFAULT_MAX_STEPS),
            max_actions=lim("max_actions", DEFAULT_MAX_ACTIONS),
            max_observations=lim("max_observations", DEFAULT_MAX_OBSERVATIONS),
            max_outcomes=lim("max_outcomes", DEFAULT_MAX_OUTCOMES),
            max_replans=lim("max_replans", DEFAULT_MAX_REPLANS),
            max_events=lim("max_events", DEFAULT_MAX_EVENTS),
            max_depth=_safe_int(limits.get("max_depth", DEFAULT_MAX_DEPTH), DEFAULT_MAX_DEPTH, 0, 32),
            default_max_retries=_safe_int(limits.get("default_max_retries", DEFAULT_MAX_RETRIES), DEFAULT_MAX_RETRIES, 0, 100),
        )
        # Snapshots can come from JSON/env adapters where booleans are
        # serialized as strings.  Parse them explicitly so ``"false"``
        # cannot accidentally enable a permissive or autonomous policy.
        ledger.require_verified_completion = _bool_flag(
            limits.get("require_verified_completion", True), True
        )
        ledger.auto_replan = _bool_flag(
            limits.get("auto_replan", False), False
        )
        raw_plans = data.get("plans", [])
        if isinstance(raw_plans, list):
            # Defer plan normalization until the full ledger has been loaded.
            # Recovery needs actions/outcomes in place before it can decide
            # whether a running step is durable or must be cancelled.
            for raw in raw_plans[-ledger.max_plans:]:
                plan = TaskPlan.from_dict(raw)
                if plan is not None:
                    ledger._store("plans", plan.id, plan, ledger.max_plans)
        for key, decoder, limit in (
            ("actions", ActionRecord.from_dict, ledger.max_actions),
            ("observations", ObservationRecord.from_dict, ledger.max_observations),
            ("outcomes", OutcomeRecord.from_dict, ledger.max_outcomes),
            ("replans", ReplanProposal.from_dict, ledger.max_replans),
        ):
            raw_items = data.get(key, [])
            if not isinstance(raw_items, list):
                continue
            collection = key
            for raw in raw_items[-limit:]:
                item = decoder(raw)
                if item is not None:
                    ledger._store(collection, item.id, item, limit)
        raw_events = data.get("events", [])
        if isinstance(raw_events, list):
            ledger.events = [_safe_mapping(item) for item in raw_events[-ledger.max_events:] if isinstance(item, Mapping)]
        stats = data.get("stats") if isinstance(data.get("stats"), Mapping) else {}
        for key, value in stats.items():
            if key:
                ledger.stats[str(key)] = _safe_int(value)
        ledger.recover_inflight(current_tick=0)
        return ledger


# Compatibility aliases used by integrations and older experiments.
TaskExecutionManager = TaskExecutionLedger
ExecutionLedger = TaskExecutionLedger


__all__ = [
    "ActionRecord", "ActionStatus", "DeterministicOutcomeEvaluator",
    "EvaluationResult", "ExecutionLedger", "ObservationRecord",
    "OutcomeQuality", "OutcomeRecord", "OutcomeStatus", "PlanStatus",
    "PlanStep", "ProvenanceRecord", "ReplanProposal", "ResultQuality",
    "StepStatus", "TaskExecutionLedger", "TaskExecutionManager", "TaskPlan",
]
