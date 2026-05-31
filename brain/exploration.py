"""Exploration — V8 自主探索循环。好奇心转化为行动。

ExplorationTask: 一个待探索的问题
ExplorationQueue: 管理任务生命周期
ExplorationExecutor: 驱动发现→执行→结论→更新的完整循环
"""

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v8.exploration")


# ══════════════════════════════════════════════
# ExplorationTask
# ══════════════════════════════════════════════

class TaskStatus:
    PENDING = "pending"
    ACTIVE = "active"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


@dataclass
class ExplorationTask:
    """一个待探索的问题。

    来源: knowledge_gap / world_model_conflict / identity_contradiction / long_term_missing
    """
    id: str
    source: str              # 来源类型
    question: str            # 要探索的问题
    goal_link: str = ""      # 关联的 Goal.id
    priority: float = 0.5    # 0-1
    created_time: str = ""
    status: str = TaskStatus.PENDING
    conclusion: str = ""     # 完成后填入
    memory_updates: list[str] = field(default_factory=list)
    attempt_count: int = 0

    def __post_init__(self):
        if not self.created_time:
            self.created_time = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "question": self.question,
            "goal_link": self.goal_link, "priority": round(self.priority, 3),
            "status": self.status, "conclusion": self.conclusion[:200],
            "created_time": self.created_time,
        }


# ══════════════════════════════════════════════
# ExplorationQueue
# ══════════════════════════════════════════════

class ExplorationQueue:
    """管理探索任务的生命周期。

    - 去重: 相同 question 不重复创建
    - 限额: 最多 20 个 pending 任务
    - 自动淘汰: 超过时限的低优先级任务自动放弃
    """

    def __init__(self):
        self.tasks: list[ExplorationTask] = []
        self.max_pending: int = 20
        self.max_age_ticks: int = 600  # 约20分钟，过期自动放弃

    def add_task(self, source: str, question: str, priority: float = 0.5) -> ExplorationTask | None:
        """添加探索任务。自动去重。"""
        # 去重
        for t in self.tasks:
            if t.question[:60] == question[:60] and t.status in (TaskStatus.PENDING, TaskStatus.ACTIVE):
                t.priority = max(t.priority, priority)
                return None  # 已存在

        # 超过限额时移除最低优先级
        pending = [t for t in self.tasks if t.status == TaskStatus.PENDING]
        if len(pending) >= self.max_pending:
            pending.sort(key=lambda t: t.priority)
            self.tasks.remove(pending[0])

        task = ExplorationTask(
            id=f"exp-{_uuid.uuid4().hex[:8]}",
            source=source,
            question=question,
            priority=priority,
        )
        self.tasks.append(task)
        logger.info("exploration: new task [%s] %s (priority=%.2f)", source, question[:60], priority)
        return task

    def get_next(self) -> ExplorationTask | None:
        """获取最高优先级的待处理任务。"""
        pending = [t for t in self.tasks if t.status == TaskStatus.PENDING]
        if not pending:
            return None
        pending.sort(key=lambda t: t.priority, reverse=True)
        return pending[0]

    def mark_active(self, task_id: str):
        for t in self.tasks:
            if t.id == task_id:
                t.status = TaskStatus.ACTIVE

    def mark_resolved(self, task_id: str, conclusion: str, memory_ids: list[str] | None = None):
        for t in self.tasks:
            if t.id == task_id:
                t.status = TaskStatus.RESOLVED
                t.conclusion = conclusion
                if memory_ids:
                    t.memory_updates = memory_ids

    def mark_abandoned(self, task_id: str, reason: str = ""):
        for t in self.tasks:
            if t.id == task_id:
                t.status = TaskStatus.ABANDONED
                t.conclusion = reason

    def tick_cleanup(self, current_tick: int):
        """清理过期任务。"""
        for t in self.tasks:
            if t.status == TaskStatus.PENDING and t.attempt_count == 0:
                try:
                    created = datetime.fromisoformat(t.created_time.replace("Z", "+00:00"))
                    age_ticks = (datetime.now(timezone.utc) - created).total_seconds() / 2.0
                    if age_ticks > self.max_age_ticks:
                        t.status = TaskStatus.ABANDONED
                        t.conclusion = "超时未处理"
                except (ValueError, TypeError):
                    pass

    def get_stats(self) -> dict:
        return {
            "total": len(self.tasks),
            "pending": sum(1 for t in self.tasks if t.status == TaskStatus.PENDING),
            "active": sum(1 for t in self.tasks if t.status == TaskStatus.ACTIVE),
            "resolved": sum(1 for t in self.tasks if t.status == TaskStatus.RESOLVED),
            "abandoned": sum(1 for t in self.tasks if t.status == TaskStatus.ABANDONED),
        }

    def snapshot(self) -> dict:
        return {
            "tasks": [t.to_dict() for t in self.tasks[-30:]],
            "stats": self.get_stats(),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "ExplorationQueue":
        eq = cls()
        for td in data.get("tasks", []):
            task = ExplorationTask(
                id=td["id"], source=td["source"], question=td["question"],
                goal_link=td.get("goal_link", ""), priority=td.get("priority", 0.5),
                status=td.get("status", TaskStatus.PENDING),
                conclusion=td.get("conclusion", ""),
            )
            task.created_time = td.get("created_time", "")
            eq.tasks.append(task)
        return eq


# ══════════════════════════════════════════════
# ExplorationExecutor
# ══════════════════════════════════════════════

class ExplorationExecutor:
    """驱动探索循环: 发现问题 → 创建任务 → 转化为目标 → 执行 → 结论 → 更新。

    不自己做任何事——它连接 curiosity/goal_system/self_model/procedural_memory。
    """

    def __init__(self):
        self.cycles_completed: int = 0
        self.total_issues_found: int = 0

    def find_issues(self, brain_stem) -> list[dict]:
        """从各信号源发现问题。

        返回: [{source, question, priority}]
        """
        issues = []

        # 1. 知识空洞 (来自 curiosity.open_questions)
        qs = brain_stem.state.curiosity.open_questions[-10:]
        for q in qs:
            issues.append({
                "source": "knowledge_gap",
                "question": q.get("question", ""),
                "priority": 0.5 + len(qs) * 0.03,  # 问题越多优先级越高
            })

        # 2. 世界模型冲突 (来自 cingulate)
        if brain_stem.state.conflict_detected:
            issues.append({
                "source": "world_model_conflict",
                "question": f"记忆冲突: {brain_stem.state.conflict_detail[:80]}",
                "priority": 0.7,
            })

        # 3. 身份矛盾 (来自 self_model identity_shifts)
        recent_shifts = brain_stem.state.self_model.identity_shifts[-3:]
        for shift in recent_shifts:
            if shift.get("significance", 0) > 0.5:
                issues.append({
                    "source": "identity_contradiction",
                    "question": f"身份变化: {shift.get('trigger', '')[:60]}",
                    "priority": 0.6 + shift.get("significance", 0) * 0.3,
                })

        # 4. 长期目标缺失 (来自 DriveEngine stagnation)
        if brain_stem.drive_engine._ticks_since_last_completion > 300:
            issues.append({
                "source": "long_term_missing",
                "question": "我长期没有完成任何目标，需要重新规划方向",
                "priority": 0.65,
            })

        self.total_issues_found += len(issues)
        return issues

    def issues_to_tasks(self, issues: list[dict], queue: ExplorationQueue) -> int:
        """将发现的问题转化为探索任务。返回新任务数。"""
        count = 0
        for issue in issues:
            task = queue.add_task(issue["source"], issue["question"], issue["priority"])
            if task:
                count += 1
        return count

    def task_to_goal(self, task: ExplorationTask, goal_generator, activation,
                     identity_traits, entities, curiosity_qs, memory_count, tick) -> Any:
        """将探索任务转化为 Goal。如果没有 goal_generator，返回 None。"""
        if goal_generator is None:
            return None

        # 用 GoalGenerator 生成一个目标
        goals = goal_generator.generate(
            activation=activation,
            identity_traits=identity_traits,
            recent_entities=entities,
            curiosity_questions=curiosity_qs,
            memory_count=memory_count,
            current_tick=tick,
        )
        return goals[0] if goals else None

    def record_cycle(self):
        self.cycles_completed += 1

    def snapshot(self) -> dict:
        return {
            "cycles_completed": self.cycles_completed,
            "total_issues_found": self.total_issues_found,
        }
