"""长期任务调度器。

该模块把已有 ``GoalSystem`` 的候选目标组织成一个更保守的长期任务层：

* 维护/连续性任务优先于用户任务，用户任务优先于探索任务；
* 队列有硬上限，满载时只淘汰最低优先级的探索任务；
* 每个任务有执行预算和绝对截止 tick；
* 高优先级任务只在当前行动到达安全边界后抢占，低优先级任务进入 paused，
  之后可恢复；
* 调度器本身不执行工具，只负责选择和持久化可观察状态。

``GoalSystem`` 仍是目标对象与历史记录的 owner，本模块只在其上增加策略层，
这样旧快照和 V5--V11 的调用方式可以继续工作。
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from brain.goal_system import Goal, GoalStatus


class TaskTier:
    """长期任务的固定优先级层级。数值越大越优先。"""

    MAINTENANCE = "maintenance"
    USER = "user"
    EXPLORATION = "exploration"

    ORDER = {
        EXPLORATION: 1,
        USER: 2,
        MAINTENANCE: 3,
    }
    ALL = frozenset(ORDER)

    @classmethod
    def normalize(cls, value: Any, default: str = EXPLORATION) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in cls.ALL else default


QUEUE_STATUSES = frozenset({
    GoalStatus.PENDING,
    GoalStatus.ACTIVE,
    GoalStatus.PAUSED,
})
TERMINAL_STATUSES = frozenset({
    GoalStatus.DONE,
    GoalStatus.FAILED,
    GoalStatus.ABANDONED,
})

DEFAULT_QUEUE_LIMIT = 12
DEFAULT_BUDGETS = {
    TaskTier.MAINTENANCE: 60,
    TaskTier.USER: 120,
    TaskTier.EXPLORATION: 90,
}
DEFAULT_DEADLINES = {
    TaskTier.MAINTENANCE: 180,
    TaskTier.USER: 360,
    TaskTier.EXPLORATION: 240,
}


def _safe_int(value: Any, default: int = 0, low: int = 0, high: int = 100000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(low, min(high, parsed))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _text(value: Any, limit: int = 500) -> str:
    return ("" if value is None else str(value)).strip()[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LongTermTaskScheduler:
    """为 GoalSystem 提供分层、有限、可恢复的长期任务策略。"""

    def __init__(
        self,
        max_queue: int = DEFAULT_QUEUE_LIMIT,
        default_budgets: dict[str, int] | None = None,
        default_deadlines: dict[str, int] | None = None,
    ):
        self.max_queue = _safe_int(max_queue, DEFAULT_QUEUE_LIMIT, 1, 100)
        self.default_budgets = self._normalize_policy(
            default_budgets, DEFAULT_BUDGETS
        )
        self.default_deadlines = self._normalize_policy(
            default_deadlines, DEFAULT_DEADLINES
        )

        self.running_goal_id: str | None = None
        self.last_selected_goal_id: str | None = None
        self.last_tick: int = 0
        self.total_preemptions: int = 0
        self.total_evicted: int = 0
        self.total_expired: int = 0
        self.total_budget_exhausted: int = 0
        self.total_user_submitted: int = 0

    @staticmethod
    def _normalize_policy(
        values: dict[str, int] | None,
        defaults: dict[str, int],
    ) -> dict[str, int]:
        result = {}
        for tier, fallback in defaults.items():
            raw = values.get(tier, fallback) if isinstance(values, dict) else fallback
            result[tier] = _safe_int(raw, fallback, 1, 100000)
        return result

    @staticmethod
    def classify_goal(goal: Goal, explicit_tier: str | None = None) -> str:
        """将旧目标映射到长期任务层级。"""
        if explicit_tier is not None:
            return TaskTier.normalize(explicit_tier)

        configured = TaskTier.normalize(
            getattr(goal, "task_tier", ""),
            default="",
        )
        # ``exploration`` is the compatibility default, so only treat a
        # non-default configured value as an explicit override here.
        if configured in {TaskTier.MAINTENANCE, TaskTier.USER}:
            return configured

        drive = _text(getattr(goal, "drive", ""), 80).lower()
        source_drive = _text(getattr(goal, "source_drive", ""), 80).lower()
        description = _text(getattr(goal, "description", ""), 240).lower()
        if (
            drive == "self_preservation"
            or source_drive == "survival_drive"
            or any(
                marker in description
                for marker in (
                    "系统稳定",
                    "连续性",
                    "核心认知",
                    "重启",
                    "维护健康",
                    "system stability",
                    "continuity",
                    "core cognition",
                    "restart",
                    "health maintenance",
                )
            )
        ):
            return TaskTier.MAINTENANCE
        return TaskTier.EXPLORATION

    @staticmethod
    def _queued(goal_system) -> list[Goal]:
        goals = goal_system.get_active()
        return [goal for goal in goals if getattr(goal, "status", "") in QUEUE_STATUSES]

    @staticmethod
    def _goal_key(goal: Goal) -> str:
        return _text(getattr(goal, "id", ""), 120)

    def _configure_goal(
        self,
        goal: Goal,
        current_tick: int,
        tier: str | None = None,
        budget_ticks: int | None = None,
        deadline_ticks: int | None = None,
        source: str | None = None,
    ) -> Goal:
        resolved_tier = self.classify_goal(goal, tier)
        goal.task_tier = resolved_tier

        existing_budget = _safe_int(getattr(goal, "budget_ticks", 0), 0, 0)
        goal.budget_ticks = _safe_int(
            budget_ticks if budget_ticks is not None else existing_budget,
            self.default_budgets[resolved_tier],
            1,
            100000,
        )
        if existing_budget <= 0 and budget_ticks is None:
            goal.budget_ticks = self.default_budgets[resolved_tier]

        existing_deadline = _safe_int(getattr(goal, "deadline_ticks", 0), 0, 0)
        if deadline_ticks is not None:
            existing_deadline = _safe_int(
                deadline_ticks,
                self.default_deadlines[resolved_tier],
                1,
                100000,
            )
            goal.deadline_ticks = existing_deadline
        elif existing_deadline <= 0:
            goal.deadline_ticks = self.default_deadlines[resolved_tier]
            existing_deadline = goal.deadline_ticks

        created_tick = _safe_int(getattr(goal, "created_tick", 0), 0, 0)
        # A task created at tick 0 is valid.  ``task_source`` acts as the
        # initialization marker so later syncs do not keep moving its age.
        if created_tick <= 0 and not getattr(goal, "task_source", ""):
            goal.created_tick = max(0, _safe_int(current_tick, 0))
        deadline_tick = _safe_int(getattr(goal, "deadline_tick", 0), 0, 0)
        if deadline_tick <= 0:
            goal.deadline_tick = max(0, _safe_int(current_tick, 0)) + existing_deadline

        goal.consumed_ticks = _safe_int(
            getattr(goal, "consumed_ticks", getattr(goal, "elapsed_ticks", 0)),
            0,
            0,
            100000,
        )
        goal.elapsed_ticks = max(
            _safe_int(getattr(goal, "elapsed_ticks", 0), 0, 0, 100000),
            goal.consumed_ticks,
        )
        if source is not None:
            goal.task_source = _text(source, 80)
        elif not getattr(goal, "task_source", ""):
            goal.task_source = "autonomous"
        return goal

    def _eviction_candidate(self, goals: list[Goal], exclude_id: str = "") -> Goal | None:
        candidates = [
            goal for goal in goals
            if self._goal_key(goal) != exclude_id
            and (
                getattr(goal, "status", "") in {GoalStatus.PENDING, GoalStatus.PAUSED}
                # An ACTIVE goal that is not the causal lane's running task
                # is a stale/legacy queue entry and may be safely evicted.
                or (
                    getattr(goal, "status", "") == GoalStatus.ACTIVE
                    and self._goal_key(goal) != self.running_goal_id
                )
            )
            and TaskTier.normalize(getattr(goal, "task_tier", "")) == TaskTier.EXPLORATION
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda goal: (
                _safe_float(getattr(goal, "priority", 0.0)),
                _safe_int(getattr(goal, "created_tick", 0)),
            ),
        )

    def register_goal(
        self,
        goal_system,
        goal: Goal,
        current_tick: int = 0,
        tier: str | None = None,
        budget_ticks: int | None = None,
        deadline_ticks: int | None = None,
        source: str | None = None,
    ) -> bool:
        """注册一个目标；队列满载时安全淘汰探索项。"""
        if goal is None or getattr(goal, "status", "") in TERMINAL_STATUSES:
            return False
        self.last_tick = max(self.last_tick, _safe_int(current_tick, 0))
        goal = self._configure_goal(
            goal,
            current_tick,
            tier=tier,
            budget_ticks=budget_ticks,
            deadline_ticks=deadline_ticks,
            source=source,
        )
        goal_id = self._goal_key(goal)
        existing = goal_system.get_by_id(goal_id) if goal_id else None
        if existing is not None:
            return True

        queued = self._queued(goal_system)
        if len(queued) >= self.max_queue:
            victim = self._eviction_candidate(queued, exclude_id=goal_id)
            if victim is None:
                return False
            if hasattr(goal_system, "abandon"):
                abandoned = goal_system.abandon(
                    victim.id,
                    f"长期任务队列已满，淘汰低优先级探索项（新任务层级={goal.task_tier}）",
                )
                if not abandoned:
                    # A legacy owner may expose ``abandon`` but reject a
                    # stale object; force the terminal marker so capacity
                    # enforcement cannot spin forever.
                    victim.status = GoalStatus.ABANDONED
            else:
                victim.status = GoalStatus.ABANDONED
            self.total_evicted += 1

        if hasattr(goal_system, "add_goal"):
            return bool(goal_system.add_goal(goal))
        raw_goals = getattr(goal_system, "_goals", None)
        if not isinstance(raw_goals, list):
            return False
        raw_goals.append(goal)
        return True

    def submit_user_goal(
        self,
        goal_system,
        description: str,
        current_tick: int = 0,
        source: str = "user",
        priority: float = 0.95,
        budget_ticks: int | None = None,
        deadline_ticks: int | None = None,
    ) -> Goal | None:
        """把一次明确的用户目标放入 user 层，并提升同名旧任务。"""
        normalized = _text(description, 500)
        if not normalized:
            return None
        fingerprint = normalized.casefold()
        for existing in self._queued(goal_system):
            if _text(getattr(existing, "description", ""), 500).casefold() == fingerprint:
                self._configure_goal(
                    existing,
                    current_tick,
                    tier=TaskTier.USER,
                    budget_ticks=budget_ticks,
                    deadline_ticks=deadline_ticks,
                    source=source,
                )
                existing.priority = max(
                    0.0,
                    min(1.0, max(_safe_float(existing.priority), _safe_float(priority, 0.95))),
                )
                if existing.status == GoalStatus.PAUSED:
                    self.resume(goal_system, existing.id)
                return existing

        resolved_deadline = deadline_ticks or self.default_deadlines[TaskTier.USER]
        goal = Goal(
            id=f"user-{uuid.uuid4().hex[:10]}",
            drive="growth",
            description=normalized,
            priority=max(0.0, min(1.0, _safe_float(priority, 0.95))),
            deadline_ticks=_safe_int(resolved_deadline, 360, 1, 100000),
            status=GoalStatus.PENDING,
            task_tier=TaskTier.USER,
            task_source=_text(source, 80) or "user",
        )
        if not self.register_goal(
            goal_system,
            goal,
            current_tick=current_tick,
            tier=TaskTier.USER,
            budget_ticks=budget_ticks,
            deadline_ticks=deadline_ticks,
            source=source,
        ):
            return None
        self.total_user_submitted += 1
        return goal

    def sync(self, goal_system, current_tick: int = 0) -> None:
        """为旧目标补齐策略字段，并清理失效的 running 指针。"""
        self.last_tick = max(self.last_tick, _safe_int(current_tick, 0))
        for goal in list(self._queued(goal_system)):
            self._configure_goal(goal, current_tick)
        if self.running_goal_id:
            running = goal_system.get_by_id(self.running_goal_id)
            # The pointer denotes the one task currently allowed to execute;
            # a paused/pending task is no longer running (for example after a
            # manually restored or externally edited snapshot).
            if (
                running is None
                or getattr(running, "status", "") not in {GoalStatus.ACTIVE}
            ):
                self.running_goal_id = None

        # Legacy callers can bypass register_goal and append directly.  Enforce
        # the same finite queue boundary during synchronization.
        queued = self._queued(goal_system)
        while len(queued) > self.max_queue:
            victim = self._eviction_candidate(queued)
            if victim is None:
                break
            if hasattr(goal_system, "abandon"):
                abandoned = goal_system.abandon(victim.id, "长期任务队列容量边界")
                if not abandoned:
                    victim.status = GoalStatus.ABANDONED
            else:
                victim.status = GoalStatus.ABANDONED
            self.total_evicted += 1
            queued = self._queued(goal_system)

    def _absolute_urgency(self, goal: Goal, current_tick: int) -> float:
        current_tick = _safe_int(current_tick, self.last_tick)
        deadline = _safe_int(getattr(goal, "deadline_tick", 0), 0)
        if deadline <= 0:
            return 0.2
        remaining = deadline - max(0, _safe_int(current_tick, 0))
        if remaining <= 0:
            return 1.0
        duration = max(
            1,
            _safe_int(getattr(goal, "deadline_ticks", 0), 1, 1, 100000),
        )
        return max(0.0, min(1.0, 1.0 - (remaining / duration)))

    def _eligible(self, goal: Goal) -> bool:
        status = getattr(goal, "status", "")
        if status not in QUEUE_STATUSES:
            return False
        if status == GoalStatus.PAUSED and _text(
            getattr(goal, "paused_reason", ""), 120
        ).startswith(("manual:", "execution:", "restart:")):
            return False
        return True

    def _score(self, goal: Goal, current_tick: int) -> tuple[float, float, float, float]:
        current_tick = _safe_int(current_tick, self.last_tick)
        tier = TaskTier.normalize(getattr(goal, "task_tier", ""))
        priority = max(0.0, min(1.0, _safe_float(getattr(goal, "priority", 0.0))))
        urgency = self._absolute_urgency(goal, current_tick)
        created = _safe_int(getattr(goal, "created_tick", 0), 0)
        age_bonus = min(0.15, max(0.0, (current_tick - created) / 1000.0))
        # Tier is the first tuple component: maintenance cannot be displaced
        # by a high-scoring exploration item.
        return (
            float(TaskTier.ORDER[tier]),
            priority * 0.60 + urgency * 0.30 + age_bonus,
            urgency,
            -float(created),
        )

    def _expire(self, goal_system, current_tick: int, episode_busy: bool) -> None:
        current_tick = _safe_int(current_tick, self.last_tick)
        for goal in list(self._queued(goal_system)):
            goal_id = self._goal_key(goal)
            if episode_busy and goal_id == self.running_goal_id:
                continue
            deadline = _safe_int(getattr(goal, "deadline_tick", 0), 0)
            consumed = _safe_int(getattr(goal, "consumed_ticks", 0), 0)
            budget = _safe_int(getattr(goal, "budget_ticks", 0), 1, 1)
            reason = ""
            budget_hit = consumed >= budget
            deadline_hit = deadline > 0 and current_tick >= deadline
            if budget_hit:
                reason = "任务执行预算已耗尽"
                self.total_budget_exhausted += 1
            elif deadline_hit:
                reason = "任务截止时间已到"
                self.total_expired += 1
            if not reason:
                continue
            if hasattr(goal_system, "mark_failed"):
                goal_system.mark_failed(goal_id, reason)
            else:
                goal.status = GoalStatus.FAILED
                goal.result_note = reason
            if goal_id == self.running_goal_id:
                self.running_goal_id = None

    def select(
        self,
        goal_system,
        current_tick: int = 0,
        episode_busy: bool = False,
    ) -> Goal | None:
        """在安全边界选择一个任务；行动进行中绝不切换。"""
        current_tick = _safe_int(current_tick, self.last_tick)
        self.sync(goal_system, current_tick)
        self._expire(goal_system, current_tick, episode_busy)
        if episode_busy:
            if self.running_goal_id:
                running = goal_system.get_by_id(self.running_goal_id)
                if running is not None and self._eligible(running):
                    return running
            return None

        candidates = [goal for goal in self._queued(goal_system) if self._eligible(goal)]
        if not candidates:
            self.running_goal_id = None
            return None
        selected = max(candidates, key=lambda goal: self._score(goal, current_tick))

        if self.running_goal_id and self.running_goal_id != self._goal_key(selected):
            previous = goal_system.get_by_id(self.running_goal_id)
            if previous is not None and getattr(previous, "status", "") == GoalStatus.ACTIVE:
                previous.status = GoalStatus.PAUSED
                previous.paused_reason = f"preempted_by:{selected.id}"
                previous.preemption_count = _safe_int(
                    getattr(previous, "preemption_count", 0), 0, 0, 100000
                ) + 1
                self.total_preemptions += 1

        selected.status = GoalStatus.ACTIVE
        if not getattr(selected, "started_at", ""):
            selected.started_at = _now_iso()
        selected.paused_reason = ""
        selected.last_run_tick = max(0, _safe_int(current_tick, 0))
        self.running_goal_id = self._goal_key(selected)
        self.last_selected_goal_id = self.running_goal_id
        self.last_tick = max(self.last_tick, _safe_int(current_tick, 0))
        return selected

    def claim_for_execution(self, goal_system, current_tick: int = 0) -> Goal | None:
        """选择并消耗一次预算，随后交给现有自治经历账本。"""
        selected = self.select(goal_system, current_tick, episode_busy=False)
        if selected is None:
            return None
        claimed = goal_system.claim(selected.id, current_tick=current_tick)
        if claimed is None:
            self.running_goal_id = None
            return None
        claimed.consumed_ticks = _safe_int(
            getattr(claimed, "consumed_ticks", 0), 0, 0, 100000
        ) + 1
        claimed.elapsed_ticks = max(
            _safe_int(getattr(claimed, "elapsed_ticks", 0), 0, 0, 100000),
            claimed.consumed_ticks,
        )
        claimed.last_run_tick = max(0, _safe_int(current_tick, 0))
        return claimed

    def pause(self, goal_system, goal_id: str, reason: str = "操作者暂停") -> bool:
        goal = goal_system.get_by_id(goal_id)
        if goal is None or getattr(goal, "status", "") in TERMINAL_STATUSES:
            return False
        goal.status = GoalStatus.PAUSED
        goal.paused_reason = f"manual:{_text(reason, 120) or '操作者暂停'}"
        if self.running_goal_id == goal_id:
            self.running_goal_id = None
        return True

    def resume(self, goal_system, goal_id: str) -> bool:
        goal = goal_system.get_by_id(goal_id)
        if goal is None or getattr(goal, "status", "") in TERMINAL_STATUSES:
            return False
        goal.status = GoalStatus.PENDING
        goal.paused_reason = ""
        if self.running_goal_id == goal_id:
            self.running_goal_id = None
        return True

    def on_goal_terminal(self, goal_id: str | None) -> None:
        if goal_id and goal_id == self.running_goal_id:
            self.running_goal_id = None

    def on_episode_closed(self, record: Any) -> None:
        """释放运行指针；中止的目标保留为可恢复状态。"""
        if record is None:
            return
        goal_id = _text(getattr(record, "goal_id", ""), 120)
        status = getattr(record, "status", "")
        if status in TERMINAL_STATUSES or status in {"completed", "failed"}:
            self.on_goal_terminal(goal_id)

    def available_slots(self, goal_system) -> int:
        self.sync(goal_system, self.last_tick)
        return max(0, self.max_queue - len(self._queued(goal_system)))

    def snapshot(self, goal_system=None) -> dict[str, Any]:
        """Return a scheduler snapshot without changing scheduler/goal state.

        ``sync`` is intentionally *not* called here.  Apart from normalising
        legacy goals, ``sync`` may evict queue entries, clear the running
        pointer, and increment eviction counters.  A snapshot is used
        by HTTP GET endpoints and diagnostics, so those callers must be able
        to observe state without introducing a write as a side effect.
        """
        queue = []
        tier_counts = {tier: 0 for tier in TaskTier.ORDER}
        if goal_system is not None:
            for goal in self._queued(goal_system)[: self.max_queue]:
                tier = TaskTier.normalize(getattr(goal, "task_tier", ""))
                tier_counts[tier] += 1
                deadline = _safe_int(getattr(goal, "deadline_tick", 0), 0)
                consumed = _safe_int(getattr(goal, "consumed_ticks", 0), 0)
                budget = _safe_int(getattr(goal, "budget_ticks", 0), 1, 1)
                queue.append({
                    "id": self._goal_key(goal),
                    "description": _text(getattr(goal, "description", ""), 180),
                    "tier": tier,
                    "status": _text(getattr(goal, "status", ""), 30),
                    "budget_ticks": budget,
                    "consumed_ticks": consumed,
                    "remaining_budget": max(0, budget - consumed),
                    "deadline_tick": deadline,
                    "paused_reason": _text(getattr(goal, "paused_reason", ""), 120),
                    "preemption_count": _safe_int(
                        getattr(goal, "preemption_count", 0), 0, 0, 100000
                    ),
                })
        return {
            "version": 1,
            "max_queue": self.max_queue,
            "running_goal_id": self.running_goal_id,
            "last_selected_goal_id": self.last_selected_goal_id,
            "last_tick": self.last_tick,
            "total_preemptions": self.total_preemptions,
            "total_evicted": self.total_evicted,
            "total_expired": self.total_expired,
            "total_budget_exhausted": self.total_budget_exhausted,
            "total_user_submitted": self.total_user_submitted,
            "tier_counts": tier_counts,
            "queue": queue,
            "defaults": {
                "budgets": dict(self.default_budgets),
                "deadlines": dict(self.default_deadlines),
            },
        }

    def read_only_snapshot(self, goal_system=None) -> dict[str, Any]:
        """Explicit read-only alias for API/diagnostic consumers.

        Keeping this as a named method makes the no-side-effect contract
        visible at call sites while preserving ``snapshot`` compatibility for
        older embedders.  Do not add lazy normalisation or expiry handling
        here; state transitions belong to the scheduler tick path (``sync`` /
        ``select``), never to a query.
        """
        return self.snapshot(goal_system)

    # ``peek`` is a concise compatibility name used by a few integrations.
    peek = read_only_snapshot

    @classmethod
    def from_snapshot(cls, data: Any) -> "LongTermTaskScheduler":
        if not isinstance(data, dict):
            return cls()
        defaults = data.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
        scheduler = cls(
            max_queue=_safe_int(data.get("max_queue", DEFAULT_QUEUE_LIMIT), DEFAULT_QUEUE_LIMIT, 1, 100),
            default_budgets=defaults.get("budgets") if isinstance(defaults.get("budgets"), dict) else None,
            default_deadlines=defaults.get("deadlines") if isinstance(defaults.get("deadlines"), dict) else None,
        )
        scheduler.running_goal_id = _text(data.get("running_goal_id", ""), 120) or None
        scheduler.last_selected_goal_id = _text(data.get("last_selected_goal_id", ""), 120) or None
        scheduler.last_tick = _safe_int(data.get("last_tick", 0))
        scheduler.total_preemptions = _safe_int(data.get("total_preemptions", 0))
        scheduler.total_evicted = _safe_int(data.get("total_evicted", 0))
        scheduler.total_expired = _safe_int(data.get("total_expired", 0))
        scheduler.total_budget_exhausted = _safe_int(data.get("total_budget_exhausted", 0))
        scheduler.total_user_submitted = _safe_int(data.get("total_user_submitted", 0))
        return scheduler
