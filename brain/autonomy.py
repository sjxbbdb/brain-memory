"""可验证的自主经历（Autonomy Episode）。

这个模块只负责记录和约束一次自主经历的生命周期，不执行工具，也不
决定外部世界应该发生什么。它把驱动力、目标、行动、反馈和收束结果
串成一个有界、可恢复的因果单元，供 BrainStem 和 AgentBridge 协作。

设计约束：
* 同一时刻最多一个活动中的自主经历，避免多个目标互相污染；
* 事件和历史都有上限，长期运行不会无限占用内存/磁盘；
* 工具参数不写入事件日志，只保留工具名和结果摘要，降低泄密风险；
* 超时、外部输入和显式失败都能收束经历，不能留下“永远等待”的状态。
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class EpisodeStatus:
    PLANNED = "planned"
    AWAITING_ACTION = "awaiting_action"
    AWAITING_FEEDBACK = "awaiting_feedback"
    FEEDBACK_RECEIVED = "feedback_received"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_STATUSES = frozenset({
    EpisodeStatus.COMPLETED,
    EpisodeStatus.FAILED,
    EpisodeStatus.ABORTED,
})

RESULT_QUALITIES = frozenset({
    "unknown",      # feedback did not describe epistemic quality
    "verified",     # result came from a local/explicitly verified source
    "simulated",    # tool returned a placeholder or dry-run observation
    "failed",       # execution/observation failed
})


def _text(value: Any, limit: int = 500) -> str:
    return ("" if value is None else str(value))[:limit]


def _number(value: Any, default: float = 0.0, low: float | None = None,
            high: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if not math.isfinite(result):
        result = default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _int(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    return max(minimum, result)


@dataclass
class EpisodeEvent:
    """一次经历中的不可变观察点。"""

    kind: str
    tick: int
    detail: str = ""
    timestamp: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.kind = _text(self.kind, 80) or "unknown"
        self.tick = _int(self.tick)
        self.detail = _text(self.detail, 300)
        self.timestamp = _text(self.timestamp, 80)
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not isinstance(self.data, dict):
            self.data = {}

    def to_dict(self) -> dict:
        # Only primitive, bounded metadata is retained.  In particular, do
        # not serialize arbitrary tool arguments or response bodies here.
        safe_data: dict[str, Any] = {}
        for key, value in list(self.data.items())[:12]:
            key = _text(key, 60)
            if isinstance(value, bool):
                safe_data[key] = value
            elif isinstance(value, (int, float)):
                try:
                    if math.isfinite(float(value)):
                        safe_data[key] = value
                except (TypeError, ValueError, OverflowError):
                    pass
            elif value is not None:
                safe_data[key] = _text(value, 160)
        return {
            "kind": self.kind,
            "tick": self.tick,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "data": safe_data,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EpisodeEvent | None":
        if not isinstance(data, dict):
            return None
        return cls(
            kind=data.get("kind", "unknown"),
            tick=data.get("tick", 0),
            detail=data.get("detail", ""),
            timestamp=_text(data.get("timestamp", ""), 80),
            data=data.get("data", {}) if isinstance(data.get("data"), dict) else {},
        )


@dataclass
class EpisodeRecord:
    """一个自主经历的当前状态和有限事件历史。"""

    id: str
    trigger: str
    goal_id: str
    goal: str
    drive: str = ""
    task_id: str = ""
    origin: str = "autonomous"
    attempt_no: int = 0
    status: str = EpisodeStatus.PLANNED
    started_tick: int = 0
    last_tick: int = 0
    ended_tick: int | None = None
    action_type: str = ""
    tool_name: str = ""
    intent_id: str = ""
    expected: str = ""
    step_count: int = 0
    success: bool | None = None
    outcome: str = ""
    last_feedback: str = ""
    result_summary: str = ""
    result_quality: str = "unknown"
    reward: float | None = None
    prediction_error: float | None = None
    memory_ids: list[str] = field(default_factory=list)
    events: list[EpisodeEvent] = field(default_factory=list)

    def __post_init__(self):
        self.id = _text(self.id, 80) or f"episode-{uuid.uuid4().hex[:12]}"
        self.trigger = _text(self.trigger, 120) or "unknown"
        self.goal_id = _text(self.goal_id, 100)
        self.goal = _text(self.goal, 300)
        self.drive = _text(self.drive, 80)
        self.task_id = _text(self.task_id, 100)
        self.origin = _text(self.origin, 60) or "autonomous"
        self.attempt_no = _int(self.attempt_no)
        self.status = _text(self.status, 40) or EpisodeStatus.PLANNED
        self.started_tick = _int(self.started_tick)
        self.last_tick = _int(self.last_tick)
        if self.ended_tick is not None:
            self.ended_tick = _int(self.ended_tick)
        self.action_type = _text(self.action_type, 60)
        self.tool_name = _text(self.tool_name, 120)
        self.intent_id = _text(self.intent_id, 80)
        self.expected = _text(self.expected, 240)
        self.step_count = _int(self.step_count)
        self.outcome = _text(self.outcome, 300)
        self.last_feedback = _text(self.last_feedback, 240)
        self.result_summary = _text(self.result_summary, 240)
        self.result_quality = _text(self.result_quality, 20).lower()
        if self.result_quality not in RESULT_QUALITIES:
            self.result_quality = "unknown"
        if self.reward is not None:
            self.reward = _number(self.reward, default=0.0, low=-1.0, high=1.0)
        if self.prediction_error is not None:
            self.prediction_error = _number(
                self.prediction_error, default=0.0, low=-1.0, high=1.0
            )
        if not isinstance(self.memory_ids, list):
            self.memory_ids = []
        else:
            self.memory_ids = [_text(item, 100) for item in self.memory_ids[-20:]]
        if not isinstance(self.events, list):
            self.events = []
        else:
            self.events = [event for event in self.events if isinstance(event, EpisodeEvent)]

    def to_dict(self, max_events: int = 64) -> dict:
        max_events = max(1, min(256, _int(max_events, 64)))
        return {
            "id": _text(self.id, 80),
            "trigger": _text(self.trigger, 120),
            "goal_id": _text(self.goal_id, 100),
            "goal": _text(self.goal, 300),
            "drive": _text(self.drive, 80),
            "task_id": _text(self.task_id, 100),
            "origin": _text(self.origin, 60),
            "attempt_no": _int(self.attempt_no),
            "status": _text(self.status, 40),
            "started_tick": _int(self.started_tick),
            "last_tick": _int(self.last_tick),
            "ended_tick": _int(self.ended_tick) if self.ended_tick is not None else None,
            "action_type": _text(self.action_type, 60),
            "tool_name": _text(self.tool_name, 120),
            "intent_id": _text(self.intent_id, 80),
            "expected": _text(self.expected, 240),
            "step_count": _int(self.step_count),
            "success": self.success if isinstance(self.success, bool) else None,
            "outcome": _text(self.outcome, 300),
            "last_feedback": _text(self.last_feedback, 240),
            "result_summary": _text(self.result_summary, 240),
            "result_quality": _text(self.result_quality, 20),
            "reward": (
                _number(self.reward, default=0.0, low=-1.0, high=1.0)
                if isinstance(self.reward, (int, float)) else None
            ),
            "prediction_error": (
                _number(self.prediction_error, default=0.0, low=-1.0, high=1.0)
                if isinstance(self.prediction_error, (int, float)) else None
            ),
            "memory_ids": [_text(item, 100) for item in self.memory_ids[-20:]]
            if isinstance(self.memory_ids, list) else [],
            "events": [
                event.to_dict() for event in self.events[-max_events:]
                if isinstance(event, EpisodeEvent)
            ] if isinstance(self.events, list) else [],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EpisodeRecord | None":
        if not isinstance(data, dict):
            return None
        record = cls(
            id=_text(data.get("id", ""), 80) or f"episode-{uuid.uuid4().hex[:12]}",
            trigger=_text(data.get("trigger", "unknown"), 120),
            goal_id=_text(data.get("goal_id", ""), 100),
            goal=_text(data.get("goal", ""), 300),
            drive=_text(data.get("drive", ""), 80),
            task_id=_text(data.get("task_id", ""), 100),
            origin=_text(data.get("origin", "autonomous"), 60),
            attempt_no=_int(data.get("attempt_no", 0)),
            status=_text(data.get("status", EpisodeStatus.PLANNED), 40),
            started_tick=_int(data.get("started_tick", 0)),
            last_tick=_int(data.get("last_tick", 0)),
            ended_tick=(
                _int(data.get("ended_tick"), minimum=0)
                if data.get("ended_tick") is not None else None
            ),
            action_type=_text(data.get("action_type", ""), 60),
            tool_name=_text(data.get("tool_name", ""), 120),
            intent_id=_text(data.get("intent_id", ""), 80),
            expected=_text(data.get("expected", ""), 240),
            step_count=_int(data.get("step_count", 0)),
            success=data.get("success") if isinstance(data.get("success"), bool) else None,
            outcome=_text(data.get("outcome", ""), 300),
            last_feedback=_text(data.get("last_feedback", ""), 240),
            result_summary=_text(data.get("result_summary", ""), 240),
            result_quality=_text(data.get("result_quality", "unknown"), 20),
            reward=(
                _number(data.get("reward"), default=0.0)
                if data.get("reward") is not None else None
            ),
            prediction_error=(
                _number(data.get("prediction_error"), default=0.0)
                if data.get("prediction_error") is not None else None
            ),
            memory_ids=(
                [_text(item, 100) for item in data.get("memory_ids", [])[-20:]]
                if isinstance(data.get("memory_ids"), list) else []
            ),
        )
        if record.status not in {
            EpisodeStatus.PLANNED,
            EpisodeStatus.AWAITING_ACTION,
            EpisodeStatus.AWAITING_FEEDBACK,
            EpisodeStatus.FEEDBACK_RECEIVED,
            *TERMINAL_STATUSES,
        }:
            record.status = EpisodeStatus.ABORTED
        raw_events = data.get("events", [])
        if isinstance(raw_events, list):
            record.events = [
                event for event in (EpisodeEvent.from_dict(item) for item in raw_events[-64:])
                if event is not None
            ]
        return record


class AutonomyEpisode:
    """管理单个自主经历，并提供快照/恢复能力。"""

    def __init__(
        self,
        max_history: int = 50,
        max_events: int = 64,
        max_ticks: int = 180,
    ):
        self.max_history = max(1, min(500, _int(max_history, 50)))
        self.max_events = max(4, min(256, _int(max_events, 64)))
        self.max_ticks = max(10, min(10000, _int(max_ticks, 180)))
        self.active: EpisodeRecord | None = None
        self.history: deque[EpisodeRecord] = deque(maxlen=self.max_history)
        self.last_closed: EpisodeRecord | None = None
        self.total_started = 0
        self.total_completed = 0
        self.total_failed = 0
        self.total_aborted = 0
        self.total_orphan_feedback = 0

    @property
    def active_id(self) -> str | None:
        return self.active.id if self.active else None

    @property
    def is_active(self) -> bool:
        return bool(self.active and self.active.status not in TERMINAL_STATUSES)

    def _append_event(
        self,
        record: EpisodeRecord,
        kind: str,
        tick: int,
        detail: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        record.events.append(EpisodeEvent(kind, tick, detail, data=data or {}))
        if len(record.events) > self.max_events:
            record.events = record.events[-self.max_events:]
        record.last_tick = max(record.last_tick, _int(tick))

    def _resolve(self, episode_id: str | None = None) -> EpisodeRecord | None:
        if self.active is None:
            return None
        # State transitions across the execution boundary must always name the
        # causal episode they belong to.  Treat a missing ID as an orphan;
        # callers that support a legacy single-lane fallback must resolve it
        # explicitly before entering this model.
        if not episode_id or str(episode_id) != self.active.id:
            return None
        return self.active

    def begin(
        self,
        goal_id: str,
        goal: str,
        drive: str = "",
        trigger: str = "drive",
        tick: int = 0,
        task_id: str = "",
        origin: str = "autonomous",
        attempt_no: int = 0,
    ) -> EpisodeRecord:
        """开始一个经历；同一目标已有经历时幂等返回原记录。"""
        goal_id = _text(goal_id, 100)
        if self.is_active:
            if self.active and self.active.goal_id == goal_id:
                self.active.last_tick = max(self.active.last_tick, _int(tick))
                return self.active
            self.abort("superseded by a new goal", tick)

        record = EpisodeRecord(
            id=f"episode-{uuid.uuid4().hex[:12]}",
            trigger=_text(trigger, 120) or "drive",
            goal_id=goal_id,
            goal=_text(goal, 300),
            drive=_text(drive, 80),
            task_id=_text(task_id, 100),
            origin=_text(origin, 60) or "autonomous",
            attempt_no=_int(attempt_no),
            started_tick=_int(tick),
            last_tick=_int(tick),
        )
        self.active = record
        self.total_started += 1
        self._append_event(record, "started", tick, "自主经历开始", {
            "goal_id": record.goal_id,
            "drive": record.drive,
            "trigger": record.trigger,
        })
        return record

    def plan(
        self,
        episode_id: str | None,
        action_type: str,
        tool_name: str = "",
        tick: int = 0,
        reason: str = "",
        intent_id: str = "",
        expected: str = "",
        attempt_no: int | None = None,
    ) -> bool:
        record = self._resolve(episode_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return False
        if record.status not in {
            EpisodeStatus.PLANNED,
            EpisodeStatus.FEEDBACK_RECEIVED,
        }:
            return False
        record.action_type = _text(action_type, 60)
        record.tool_name = _text(tool_name, 120)
        record.intent_id = _text(intent_id, 80)
        record.expected = _text(expected, 240)
        if attempt_no is not None:
            record.attempt_no = _int(attempt_no)
        record.step_count += 1
        record.status = EpisodeStatus.AWAITING_ACTION
        self._append_event(record, "planned", tick, _text(reason, 240), {
            "action_type": record.action_type,
            "tool_name": record.tool_name,
            "step": record.step_count,
            "intent_id": record.intent_id,
        })
        return True

    def action_started(self, episode_id: str | None, tick: int = 0) -> bool:
        record = self._resolve(episode_id)
        if record is None or record.status in TERMINAL_STATUSES:
            return False
        # Reject duplicate/stale deliveries.  A tool must be claimed exactly
        # once from a planned state; otherwise a queued copy could execute
        # again after the episode has already moved to feedback.
        if record.status not in {
            EpisodeStatus.PLANNED,
            EpisodeStatus.AWAITING_ACTION,
        }:
            return False
        record.status = EpisodeStatus.AWAITING_FEEDBACK
        self._append_event(record, "action_started", tick, "行动已交给执行边界", {
            "tool_name": record.tool_name,
            "step": record.step_count,
            "intent_id": record.intent_id,
        })
        return True

    def record_feedback(
        self,
        episode_id: str | None,
        success: bool,
        tool_name: str = "",
        tick: int = 0,
        intent_id: str = "",
        result_summary: str = "",
        result_quality: str | None = None,
        reward: float | None = None,
        prediction_error: float | None = None,
    ) -> bool:
        """记录工具反馈；不接受来自其他经历的迟到反馈。"""
        record = self._resolve(episode_id)
        if record is None or record.status in TERMINAL_STATUSES:
            self.total_orphan_feedback += 1
            return False
        if record.status not in {
            EpisodeStatus.AWAITING_ACTION,
            EpisodeStatus.AWAITING_FEEDBACK,
        }:
            self.total_orphan_feedback += 1
            return False
        normalized_tool = _text(tool_name, 120)
        if normalized_tool and record.tool_name and normalized_tool != record.tool_name:
            self.total_orphan_feedback += 1
            return False
        if normalized_tool:
            record.tool_name = normalized_tool
        if intent_id and record.intent_id and intent_id != record.intent_id:
            self.total_orphan_feedback += 1
            return False
        if intent_id:
            record.intent_id = _text(intent_id, 80)
        record.success = bool(success)
        record.status = EpisodeStatus.FEEDBACK_RECEIVED
        record.last_feedback = "success" if success else "failure"
        record.result_summary = _text(result_summary or record.last_feedback, 240)
        if result_quality is not None:
            quality = _text(result_quality, 20).lower()
            record.result_quality = quality if quality in RESULT_QUALITIES else "unknown"
        elif not success:
            record.result_quality = "failed"
        elif record.result_quality == "unknown":
            record.result_quality = "verified"
        if reward is not None:
            record.reward = _number(reward, default=0.0, low=-1.0, high=1.0)
        if prediction_error is not None:
            record.prediction_error = _number(
                prediction_error, default=0.0, low=-1.0, high=1.0
            )
        self._append_event(record, "feedback", tick, record.last_feedback, {
            "tool_name": record.tool_name,
            "success": bool(success),
            "quality": record.result_quality,
            "step": record.step_count,
            "intent_id": record.intent_id,
        })
        return True

    def close(
        self,
        success: bool,
        outcome: str = "",
        tick: int = 0,
        status: str | None = None,
        reward: float | None = None,
        prediction_error: float | None = None,
        memory_ids: list[str] | None = None,
    ) -> EpisodeRecord | None:
        record = self.active
        if record is None:
            return None
        if record.status in TERMINAL_STATUSES:
            return record
        final_status = status or (
            EpisodeStatus.COMPLETED if success else EpisodeStatus.FAILED
        )
        if final_status not in TERMINAL_STATUSES:
            final_status = EpisodeStatus.COMPLETED if success else EpisodeStatus.FAILED
        record.success = bool(success) if final_status != EpisodeStatus.ABORTED else False
        record.status = final_status
        record.outcome = _text(outcome, 300)
        if final_status == EpisodeStatus.FAILED and record.result_quality == "unknown":
            record.result_quality = "failed"
        if reward is not None:
            record.reward = _number(reward, default=0.0, low=-1.0, high=1.0)
        if prediction_error is not None:
            record.prediction_error = _number(
                prediction_error, default=0.0, low=-1.0, high=1.0
            )
        if memory_ids:
            record.memory_ids = [_text(item, 100) for item in memory_ids[-20:]]
        record.ended_tick = _int(tick)
        self._append_event(record, "closed", tick, record.outcome, {
            "success": bool(record.success),
            "status": final_status,
        })
        self.last_closed = record
        self.history.append(record)
        self.active = None
        if final_status == EpisodeStatus.COMPLETED:
            self.total_completed += 1
        elif final_status == EpisodeStatus.FAILED:
            self.total_failed += 1
        else:
            self.total_aborted += 1
        return record

    def complete(self, outcome: str = "", tick: int = 0, **kwargs) -> EpisodeRecord | None:
        return self.close(True, outcome, tick, EpisodeStatus.COMPLETED, **kwargs)

    def fail(self, outcome: str = "", tick: int = 0, **kwargs) -> EpisodeRecord | None:
        return self.close(False, outcome, tick, EpisodeStatus.FAILED, **kwargs)

    def abort(self, reason: str = "aborted", tick: int = 0) -> EpisodeRecord | None:
        return self.close(False, reason, tick, EpisodeStatus.ABORTED)

    def tick(self, current_tick: int) -> EpisodeRecord | None:
        """超时收束活动经历，返回刚收束的记录。"""
        if not self.is_active or self.active is None:
            return None
        if _int(current_tick) - self.active.last_tick >= self.max_ticks:
            return self.fail("自主经历超时，已安全收束", current_tick)
        return None

    def snapshot(self) -> dict:
        current = self.active.to_dict(self.max_events) if self.active else None
        last = self.last_closed.to_dict(self.max_events) if self.last_closed else None
        return {
            "version": 1,
            "active": current,
            "last_closed": last,
            "history": [record.to_dict(self.max_events) for record in self.history],
            "limits": {
                "max_history": self.max_history,
                "max_events": self.max_events,
                "max_ticks": self.max_ticks,
            },
            "stats": {
                "total_started": self.total_started,
                "total_completed": self.total_completed,
                "total_failed": self.total_failed,
                "total_aborted": self.total_aborted,
                "total_orphan_feedback": self.total_orphan_feedback,
            },
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> "AutonomyEpisode":
        if not isinstance(data, dict):
            return cls()
        limits = data.get("limits", {}) if isinstance(data.get("limits"), dict) else {}
        manager = cls(
            max_history=_int(limits.get("max_history", 50), 50),
            max_events=_int(limits.get("max_events", 64), 64),
            max_ticks=_int(limits.get("max_ticks", 180), 180),
        )
        active = EpisodeRecord.from_dict(data.get("active"))
        if active and active.status not in TERMINAL_STATUSES:
            manager.active = active
        last = EpisodeRecord.from_dict(data.get("last_closed"))
        if last and last.status in TERMINAL_STATUSES:
            manager.last_closed = last
        raw_history = data.get("history", [])
        if isinstance(raw_history, list):
            for item in raw_history[-manager.max_history:]:
                record = EpisodeRecord.from_dict(item)
                if record:
                    manager.history.append(record)
        stats = data.get("stats", {}) if isinstance(data.get("stats"), dict) else {}
        manager.total_started = _int(stats.get("total_started", 0))
        manager.total_completed = _int(stats.get("total_completed", 0))
        manager.total_failed = _int(stats.get("total_failed", 0))
        manager.total_aborted = _int(stats.get("total_aborted", 0))
        manager.total_orphan_feedback = _int(stats.get("total_orphan_feedback", 0))
        return manager

    def summary(self) -> dict:
        """用于 API/状态面板的短摘要，不暴露工具参数。"""
        active = self.active
        return {
            "active": active.to_dict(self.max_events) if active else None,
            "last_closed_id": self.last_closed.id if self.last_closed else "",
            "status": active.status if active else "idle",
            "total_started": self.total_started,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "total_aborted": self.total_aborted,
            "total_orphan_feedback": self.total_orphan_feedback,
        }
