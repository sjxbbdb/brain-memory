"""Verified outcome -> learning feedback boundary (V13).

This module is deliberately kept separate from :mod:`brain_stem` and from
the action executor.  An executor may report that a call returned, but a
returning call is not the same thing as a verified result.  The boundary here
only lets an explicitly verified terminal outcome change the learning
subsystems.

The public entry point is :meth:`VerifiedLearningFeedback.apply`::

    receipt = feedback.apply(
        outcome,
        procedural_memory=procedural,
        reward_system=rewards,
        self_model=self_model,
        memory_store=memory_store,
    )

``outcome`` may be a mapping or an object such as ``AutonomyEpisode``'s
``EpisodeRecord``.  The adapter is intentionally duck-typed so this module
does not create a dependency on a particular execution implementation.

Safety rules:

* ``quality=verified`` and ``success=True`` are required for positive
  learning;
* ``quality=unknown`` and ``quality=simulated`` are quarantined, even when a
  caller says ``success=True``;
* an explicit, terminal failure (``quality=failed`` or verified false) may
  weaken a skill and deliver a low reward, but can never strengthen one;
* one causal outcome id is applied at most once per runtime/snapshot; and
* only bounded, redacted summaries are written to memory.  Raw tool
  arguments, evidence bodies, and credentials are never copied.

The existing ``ReflectionEngine`` exposes a periodic whole-brain ``reflect``
method rather than an outcome-ingestion method.  For that reason this module
stores a compact ``type=reflection`` memory and updates the public
``SelfModel.integrate_reflection`` hook.  If a future reflection engine adds a
``record_learning_reflection`` hook, it is called opportunistically without
making that hook a requirement today.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger("brain-v13.learning-feedback")


class LearningDisposition:
    """Stable disposition strings returned in :class:`LearningReceipt`."""

    POSITIVE = "verified_success"
    NEGATIVE = "verified_failure"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


_QUALITY_ALIASES = {
    "verified": "verified",
    "verify": "verified",
    "validated": "verified",
    "confirmed": "verified",
    "passed": "verified",
    "pass": "verified",
    "simulated": "simulated",
    "simulation": "simulated",
    "dry_run": "simulated",
    "dry-run": "simulated",
    "unknown": "unknown",
    "unverified": "unknown",
    "uncertain": "unknown",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "rejected": "failed",
}

_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "succeeded",
        "success",
        "failed",
        "failure",
        "done",
        "verified",
    }
)

_KNOWN_REWARD_CHANNELS = frozenset(
    {"cognitive", "social", "achievement", "novelty", "aesthetic"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 240) -> str:
    """Coerce text and remove control characters at the boundary."""

    if value is None:
        return ""
    value = str(value).replace("\x00", " ")
    value = " ".join(value.split())
    return value[: max(0, int(limit))]


# These patterns target common credential forms without attempting to inspect
# arbitrary evidence.  The learning ledger only receives already-short text.
_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|token)\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_\-]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED]"),
)


def _safe_text(value: Any, limit: int = 240) -> str:
    text = _text(value, limit)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[: max(0, int(limit))]


def _number(value: Any, default: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if not math.isfinite(result):
        result = default
    return max(low, min(high, result))


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "success", "succeeded", "passed"}:
            return True
        if normalized in {"false", "0", "no", "failure", "failed", "error"}:
            return False
    return None


def _mapping_for(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, Mapping):
                return dict(result)
        except Exception:
            pass
    try:
        raw = vars(value)
        if isinstance(raw, dict):
            return dict(raw)
    except (TypeError, AttributeError):
        pass
    return {}


def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


@dataclass(frozen=True)
class LearningOutcome:
    """Normalized, bounded input accepted by the feedback boundary."""

    outcome_id: str
    success: bool | None
    quality: str = "unknown"
    status: str = ""
    task_id: str = ""
    episode_id: str = ""
    step_id: str = ""
    goal: str = ""
    intent_type: str = "call_tool"
    tool_name: str = ""
    summary: str = ""
    skill_id: str = ""
    channel: str = "achievement"
    confidence: float = 1.0
    verification_method: str = ""
    timestamp: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "LearningOutcome":
        data = _mapping_for(value)
        verification = data.get("verification")
        if isinstance(verification, Mapping):
            # Explicit top-level fields always win over nested evaluator
            # metadata; nested evidence itself is intentionally discarded.
            merged = dict(verification)
            merged.update(data)
            data = merged

        outcome_id = _safe_text(
            _first(data, "outcome_id", "evaluation_id", "event_id", "id"), 120
        )
        episode_id = _safe_text(_first(data, "episode_id", "episode"), 120)
        task_id = _safe_text(_first(data, "task_id", "task"), 120)
        step_id = _safe_text(_first(data, "step_id", "step", "intent_id"), 120)
        tool_name = _safe_text(_first(data, "tool_name", "tool", "action"), 100)
        attempt = _safe_text(_first(data, "attempt_no", "attempt"), 30)

        # A final EpisodeRecord has a stable id.  For evaluators that only
        # expose task/step IDs, derive a stable causal key rather than using a
        # wall-clock value (which would defeat duplicate protection).
        if not outcome_id:
            parts = [episode_id, task_id, step_id, attempt, tool_name]
            if any(parts):
                outcome_id = "derived-" + hashlib.sha256(
                    "|".join(parts).encode("utf-8", "replace")
                ).hexdigest()[:24]

        raw_quality = _first(
            data,
            "quality",
            "result_quality",
            "verification_quality",
            "verifier_status",
            default="unknown",
        )
        quality = _QUALITY_ALIASES.get(_text(raw_quality, 30).lower(), "unknown")

        verified_flag = _first(data, "verified", "verification_passed")
        parsed_verified = _parse_bool(verified_flag)
        if parsed_verified is True and raw_quality in (None, "", "unknown"):
            quality = "verified"
        elif parsed_verified is False and raw_quality in (None, ""):
            quality = "unknown"

        raw_success = _first(data, "success", "succeeded", "ok")
        success = _parse_bool(raw_success)
        if success is None and quality == "failed":
            # A failed evaluator result is an explicit negative observation.
            success = False

        status = _safe_text(_first(data, "status", "state"), 40).lower().replace(" ", "_")
        terminal = _first(data, "terminal", "is_terminal")
        if terminal is False or _parse_bool(terminal) is False:
            status = "non_terminal"

        return cls(
            outcome_id=outcome_id,
            success=success,
            quality=quality,
            status=status,
            task_id=task_id,
            episode_id=episode_id,
            step_id=step_id,
            goal=_safe_text(_first(data, "goal", "objective", "description"), 240),
            intent_type=_safe_text(_first(data, "intent_type", "action_type"), 60)
            or "call_tool",
            tool_name=tool_name,
            summary=_safe_text(
                _first(data, "summary", "result_summary", "outcome", "message"), 240
            ),
            skill_id=_safe_text(_first(data, "skill_id", "procedure_id"), 120),
            channel=_safe_text(_first(data, "channel", "reward_channel"), 40).lower()
            or "achievement",
            confidence=_number(
                _first(data, "confidence", "verification_confidence", default=1.0),
                1.0,
            ),
            verification_method=_safe_text(
                _first(data, "verification_method", "verified_by", "method"), 100
            ),
            timestamp=_safe_text(_first(data, "timestamp", "created", default=""), 80)
            or _now(),
        )


@dataclass
class LearningReceipt:
    """Bounded audit result for one call to :meth:`apply`."""

    outcome_id: str
    disposition: str
    quality: str = "unknown"
    success: bool | None = None
    learned: bool = False
    reason: str = ""
    actions: list[str] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reflection: str = ""
    timestamp: str = field(default_factory=_now)

    def to_dict(self, include_details: bool = True) -> dict[str, Any]:
        result = {
            "outcome_id": _safe_text(self.outcome_id, 120),
            "disposition": _safe_text(self.disposition, 40),
            "quality": _safe_text(self.quality, 20),
            "success": self.success if isinstance(self.success, bool) else None,
            "learned": bool(self.learned),
            "reason": _safe_text(self.reason, 180),
            "actions": [_safe_text(x, 80) for x in self.actions[-20:]],
            "memory_ids": [_safe_text(x, 120) for x in self.memory_ids[-10:]],
            "errors": [_safe_text(x, 160) for x in self.errors[-10:]],
            "timestamp": _safe_text(self.timestamp, 80),
        }
        if include_details:
            result["reflection"] = _safe_text(self.reflection, 300)
        return result

    @classmethod
    def from_dict(cls, data: Any) -> "LearningReceipt | None":
        if not isinstance(data, Mapping):
            return None
        success = data.get("success")
        return cls(
            outcome_id=_safe_text(data.get("outcome_id", ""), 120),
            disposition=_safe_text(data.get("disposition", LearningDisposition.REJECTED), 40),
            quality=_safe_text(data.get("quality", "unknown"), 20),
            success=success if isinstance(success, bool) else None,
            learned=bool(data.get("learned", False)),
            reason=_safe_text(data.get("reason", ""), 180),
            actions=[_safe_text(x, 80) for x in data.get("actions", []) if x is not None][-20:]
            if isinstance(data.get("actions"), list)
            else [],
            memory_ids=[_safe_text(x, 120) for x in data.get("memory_ids", []) if x is not None][-10:]
            if isinstance(data.get("memory_ids"), list)
            else [],
            errors=[_safe_text(x, 160) for x in data.get("errors", []) if x is not None][-10:]
            if isinstance(data.get("errors"), list)
            else [],
            reflection=_safe_text(data.get("reflection", ""), 300),
            timestamp=_safe_text(data.get("timestamp", ""), 80) or _now(),
        )


class VerifiedLearningFeedback:
    """Apply only trustworthy terminal outcomes to learning components.

    The class has no database connection of its own.  This keeps it safe to
    instantiate alongside old ``BrainStem`` versions and lets the caller
    choose the existing ``MemoryStore``/snapshot lifecycle.
    """

    def __init__(self, max_history: int = 100, max_seen: int = 500):
        self.max_history = max(1, min(1000, int(max_history)))
        self.max_seen = max(1, min(5000, int(max_seen)))
        self._seen_ids: deque[str] = deque()
        self._seen_set: set[str] = set()
        self.history: deque[LearningReceipt] = deque(maxlen=self.max_history)
        self.total_processed = 0
        self.total_positive = 0
        self.total_negative = 0
        self.total_quarantined = 0
        self.total_rejected = 0
        self.total_duplicates = 0
        self.component_errors = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(
        self,
        outcome: Any,
        procedural_memory: Any = None,
        reward_system: Any = None,
        self_model: Any = None,
        memory_store: Any = None,
        reflection_engine: Any = None,
        current_tick: int | None = None,
    ) -> LearningReceipt:
        """Classify and apply one outcome, returning a bounded receipt.

        All component calls are best-effort and isolated.  A broken optional
        component is reported in ``receipt.errors`` but cannot crash the
        heartbeat that delivered the outcome.
        """

        normalized = LearningOutcome.from_value(outcome)
        with self._lock:
            self.total_processed += 1

            if not normalized.outcome_id:
                self.total_rejected += 1
                return self._finish(
                    LearningReceipt(
                        outcome_id="",
                        disposition=LearningDisposition.REJECTED,
                        quality=normalized.quality,
                        success=normalized.success,
                        reason="缺少稳定的 outcome_id/episode_id/task_id，拒绝写入学习层",
                    )
                )

            if normalized.outcome_id in self._seen_set:
                self.total_duplicates += 1
                return self._finish(
                    LearningReceipt(
                        outcome_id=normalized.outcome_id,
                        disposition=LearningDisposition.DUPLICATE,
                        quality=normalized.quality,
                        success=normalized.success,
                        reason="同一因果结果已处理，拒绝重复强化",
                    )
                )

            disposition, reason = self._classify(normalized)
            if disposition == LearningDisposition.REJECTED:
                self.total_rejected += 1
                return self._finish(
                    LearningReceipt(
                        outcome_id=normalized.outcome_id,
                        disposition=disposition,
                        quality=normalized.quality,
                        success=normalized.success,
                        reason=reason,
                    )
                )

            # Mark before component calls.  This prevents a retry storm from
            # repeatedly strengthening a skill if one optional sink fails.
            self._remember(normalized.outcome_id)

            if disposition == LearningDisposition.QUARANTINED:
                self.total_quarantined += 1
                return self._finish(
                    LearningReceipt(
                        outcome_id=normalized.outcome_id,
                        disposition=disposition,
                        quality=normalized.quality,
                        success=normalized.success,
                        reason=reason,
                    )
                )

            positive = disposition == LearningDisposition.POSITIVE
            if positive:
                self.total_positive += 1
            else:
                self.total_negative += 1

            receipt = LearningReceipt(
                outcome_id=normalized.outcome_id,
                disposition=disposition,
                quality=normalized.quality,
                success=normalized.success,
                learned=False,
                reason=reason,
            )
            self._apply_procedural(normalized, positive, procedural_memory, receipt)
            self._apply_reward(normalized, positive, reward_system, receipt)
            self._apply_memory_and_reflection(
                normalized,
                positive,
                self_model,
                memory_store,
                reflection_engine,
                current_tick,
                receipt,
            )
            receipt.learned = bool(receipt.actions)
            return self._finish(receipt)

    def ingest_verified_outcome(self, outcome: Any, **components: Any) -> LearningReceipt:
        """Descriptive alias for integrations that prefer an ingestion name."""

        return self.apply(outcome, **components)

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe state without copying task/result text."""

        with self._lock:
            return {
                "version": 1,
                "limits": {"max_history": self.max_history, "max_seen": self.max_seen},
                "stats": {
                    "total_processed": self.total_processed,
                    "total_positive": self.total_positive,
                    "total_negative": self.total_negative,
                    "total_quarantined": self.total_quarantined,
                    "total_rejected": self.total_rejected,
                    "total_duplicates": self.total_duplicates,
                    "component_errors": self.component_errors,
                },
                "seen_ids": list(self._seen_ids)[-self.max_seen :],
                "history": [r.to_dict(include_details=False) for r in self.history],
            }

    @classmethod
    def from_snapshot(cls, data: Any) -> "VerifiedLearningFeedback":
        if not isinstance(data, Mapping):
            return cls()
        limits = data.get("limits") if isinstance(data.get("limits"), Mapping) else {}
        try:
            max_history = int(limits.get("max_history", 100))
        except (TypeError, ValueError, OverflowError):
            max_history = 100
        try:
            max_seen = int(limits.get("max_seen", 500))
        except (TypeError, ValueError, OverflowError):
            max_seen = 500
        engine = cls(max_history=max_history, max_seen=max_seen)

        raw_seen = data.get("seen_ids", [])
        if isinstance(raw_seen, list):
            for item in raw_seen[-engine.max_seen :]:
                item = _safe_text(item, 120)
                if item:
                    engine._remember(item)

        stats = data.get("stats") if isinstance(data.get("stats"), Mapping) else {}
        for name in (
            "total_processed",
            "total_positive",
            "total_negative",
            "total_quarantined",
            "total_rejected",
            "total_duplicates",
            "component_errors",
        ):
            try:
                setattr(engine, name, max(0, int(stats.get(name, 0))))
            except (TypeError, ValueError, OverflowError):
                pass

        raw_history = data.get("history", [])
        if isinstance(raw_history, list):
            for raw in raw_history[-engine.max_history :]:
                receipt = LearningReceipt.from_dict(raw)
                if receipt is not None:
                    engine.history.append(receipt)
        return engine

    # ------------------------------------------------------------------
    # Classification and component adapters
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(outcome: LearningOutcome) -> tuple[str, str]:
        if outcome.success is None:
            return LearningDisposition.REJECTED, "结果缺少明确的 success 布尔值"
        if outcome.status and outcome.status not in _TERMINAL_STATUSES:
            return LearningDisposition.QUARANTINED, "结果尚未处于终态，不进入学习层"
        if outcome.quality in {"unknown", "simulated"}:
            return LearningDisposition.QUARANTINED, "结果未被外部事实验证，保持隔离"
        if outcome.quality == "failed" and outcome.success:
            return LearningDisposition.QUARANTINED, "quality=failed 与 success=True 矛盾，保持隔离"
        if outcome.quality == "verified" and outcome.success:
            return LearningDisposition.POSITIVE, "已验证的终态成功，可形成正向学习"
        if outcome.quality in {"verified", "failed"} and not outcome.success:
            return LearningDisposition.NEGATIVE, "已确认的终态失败，只允许负向校准"
        return LearningDisposition.QUARANTINED, "结果质量不足以支持学习"

    def _remember(self, outcome_id: str) -> None:
        if outcome_id in self._seen_set:
            return
        self._seen_ids.append(outcome_id)
        self._seen_set.add(outcome_id)
        while len(self._seen_ids) > self.max_seen:
            old = self._seen_ids.popleft()
            self._seen_set.discard(old)

    def _finish(self, receipt: LearningReceipt) -> LearningReceipt:
        self.history.append(receipt)
        return receipt

    def _error(self, receipt: LearningReceipt, component: str, exc: Exception) -> None:
        self.component_errors += 1
        receipt.errors.append(f"{component}: {_safe_text(str(exc), 140)}")
        logger.warning("learning feedback %s failed: %s", component, str(exc)[:120])

    def _apply_procedural(
        self,
        outcome: LearningOutcome,
        positive: bool,
        procedural_memory: Any,
        receipt: LearningReceipt,
    ) -> None:
        if procedural_memory is None:
            return
        try:
            # Empty tool names are allowed for non-tool episodes, but they do
            # not create useful reusable skills and should not pollute the
            # procedural experience buffer.
            if outcome.tool_name or outcome.intent_type not in {"", "call_tool"}:
                procedural_memory.record_experience(
                    outcome.intent_type or "call_tool",
                    outcome.tool_name,
                    positive,
                    _safe_text(outcome.goal, 200),
                    _safe_text(outcome.summary, 200),
                )
                receipt.actions.append("procedural.record_experience")
            if outcome.skill_id:
                reinforce = getattr(procedural_memory, "reinforce_skill", None)
                if callable(reinforce):
                    reinforce(outcome.skill_id, positive)
                    receipt.actions.append("procedural.reinforce_skill")
        except Exception as exc:
            self._error(receipt, "procedural_memory", exc)

    def _apply_reward(
        self,
        outcome: LearningOutcome,
        positive: bool,
        reward_system: Any,
        receipt: LearningReceipt,
    ) -> None:
        if reward_system is None:
            return
        try:
            channel = outcome.channel if outcome.channel in _KNOWN_REWARD_CHANNELS else "achievement"
            actual_reward = (
                min(0.95, 0.72 + 0.20 * outcome.confidence) if positive else 0.12
            )
            reward_system.deliver_reward(
                channel=channel,
                actual_reward=actual_reward,
                context=f"verified-learning:{_safe_text(outcome.tool_name or 'task', 60)}",
            )
            receipt.actions.append("reward.deliver_reward")
        except Exception as exc:
            self._error(receipt, "reward_system", exc)

    def _apply_memory_and_reflection(
        self,
        outcome: LearningOutcome,
        positive: bool,
        self_model: Any,
        memory_store: Any,
        reflection_engine: Any,
        current_tick: int | None,
        receipt: LearningReceipt,
    ) -> None:
        label = "已验证成功" if positive else "已确认失败"
        safe_goal = _safe_text(outcome.goal or outcome.task_id or "未命名任务", 180)
        safe_tool = _safe_text(outcome.tool_name or outcome.intent_type or "行动", 80)
        safe_summary = _safe_text(outcome.summary or "无摘要", 180)
        if positive:
            reflection = (
                f"{label}：{safe_goal}。行动 {safe_tool} 的结果经过"
                f"{_safe_text(outcome.verification_method or '明确验证', 60)}确认；"
                "可保留为可复用经验，但不自动扩展为普遍事实。"
            )
            emotion_label = "breakthrough"
            emotion_vector = {"valence": 0.76, "arousal": 0.58, "dominance": 0.66}
            importance = 0.62
        else:
            reflection = (
                f"{label}：{safe_goal}。行动 {safe_tool} 未达成目标，"
                "本次结果只用于削弱相关策略，不作为成功范例。"
            )
            emotion_label = "failure"
            emotion_vector = {"valence": 0.24, "arousal": 0.55, "dominance": 0.34}
            importance = 0.48
        receipt.reflection = reflection[:300]

        if memory_store is not None:
            try:
                memory_id = "learning-" + hashlib.sha256(
                    outcome.outcome_id.encode("utf-8", "replace")
                ).hexdigest()[:24]
                memory = {
                    "id": memory_id,
                    "type": "reflection",
                    "title": f"{label}学习记录",
                    "content": (
                        f"{label} | 目标: {safe_goal} | 行动: {safe_tool} | "
                        f"结果摘要: {safe_summary} | {reflection}"
                    )[:900],
                    "summary": reflection[:200],
                    "source": "verified_learning_feedback",
                    "importance": importance,
                    "emotion_label": emotion_label,
                    "emotion_vector": dict(emotion_vector),
                    "entities": ["验证结果", "学习反馈"],
                    "emotion_tags": ["verified", "learning"],
                    "is_identity_forming": False,
                    "created": outcome.timestamp or _now(),
                }
                saved_id = memory_store.save(memory)
                if saved_id:
                    receipt.memory_ids.append(_safe_text(saved_id, 120))
                    receipt.actions.append("memory.save_reflection")
            except Exception as exc:
                self._error(receipt, "memory_store", exc)

        if self_model is not None:
            try:
                ingest = getattr(self_model, "ingest_experience", None)
                if callable(ingest):
                    count = 0
                    if memory_store is not None:
                        count_fn = getattr(memory_store, "count", None)
                        if callable(count_fn):
                            try:
                                count = max(0, int(count_fn()))
                            except (TypeError, ValueError, OverflowError):
                                count = 0
                    ingest(
                        text=f"[{label}] {safe_goal}: {safe_summary}"[:420],
                        emotion={
                            "emotion_label": emotion_label,
                            "emotion_vector": emotion_vector,
                            "salience": 0.68 if positive else 0.52,
                        },
                        importance=importance,
                        memory_count=count,
                    )
                    receipt.actions.append("self_model.ingest_experience")
                integrate = getattr(self_model, "integrate_reflection", None)
                if callable(integrate):
                    integrate({"thought": receipt.reflection})
                    receipt.actions.append("self_model.integrate_reflection")
            except Exception as exc:
                self._error(receipt, "self_model", exc)

        # ReflectionEngine currently has only a whole-brain ``reflect`` API.
        # Avoid calling it with a fake BrainStem; use a future narrow hook if
        # one is supplied by a newer implementation.
        if reflection_engine is not None:
            try:
                hook = getattr(reflection_engine, "record_learning_reflection", None)
                if callable(hook):
                    hook(
                        receipt.reflection,
                        outcome_id=outcome.outcome_id,
                        verified=positive,
                        current_tick=current_tick,
                    )
                    receipt.actions.append("reflection_engine.record_learning_reflection")
            except Exception as exc:
                self._error(receipt, "reflection_engine", exc)


# Short alias for callers that do not need to emphasize the verification
# boundary in their local variable names.
LearningFeedback = VerifiedLearningFeedback


__all__ = [
    "LearningDisposition",
    "LearningFeedback",
    "LearningOutcome",
    "LearningReceipt",
    "VerifiedLearningFeedback",
]
