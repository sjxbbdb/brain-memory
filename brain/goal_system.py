"""Goal System — v5.1 目标引擎。让大脑自己找事做。

v5.0 有驱动力但没有目标——好奇、想成长、要一致，但从不行动。
目标引擎把驱动力转化为具体的、可执行的目标。

原则:
- 驱动力是"为什么"，目标是"做什么"
- 最多 3 个活跃目标，防止注意分散
- 目标有时限，逾期自动失败
- 完成/失败的目标写入自我叙事，微调驱动力权重
- 闲置时大脑可以自主推进目标，不依赖外部输入

生命周期:
  驱动力+好奇心+记忆空白 → 生成目标 → 活跃执行 → 完成/失败/放弃
                                    ↓
                           intent(CALL_TOOL) → AgentBridge
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v5.goal-system")


# ── 驱动 → 目标模板映射 ──
DRIVE_GOAL_TEMPLATES = {
    "curiosity": [
        "搜索并学习关于 {entity} 的更多信息",
        "搞清楚 {entity} 的运作原理",
        "探索 {entity} 和相关概念之间的关联",
        "查看关于 {entity} 的最新资料",
    ],
    "coherence": [
        "检查记忆中是否有与 {entity} 矛盾的记录",
        "验证关于 {entity} 的现有认知是否正确",
        "整理关于 {entity} 的碎片化记忆",
    ],
    "growth": [
        "把关于 {entity} 的经验总结成一个可复用的方法",
        "将最近学到的 {entity} 知识应用到实践中",
        "掌握 {entity} 的核心要点",
    ],
    "connection": [
        "了解调用者此刻需要什么帮助",
        "回顾最近的对话，找出未完成的事项",
        "主动提供关于 {entity} 的信息",
    ],
    "self_preservation": [
        "检查记忆库健康状态",
        "回顾最近的身份变化是否合理",
        "确认核心认知没有被最近的输入动摇",
    ],
}

# 目标状态
class GoalStatus:
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass
class Goal:
    """大脑为自己设定的一个具体目标。V7: +survival_gain/growth_gain/identity_gain/source_drive。"""
    id: str
    drive: str                # 来源驱动力: curiosity/coherence/growth/connection/self_preservation
    description: str          # 自然语言描述
    priority: float           # 0.0-1.0
    deadline_ticks: int       # 预期完成时间（tick数，0=无时限）
    elapsed_ticks: int = 0    # 已消耗 tick 数
    progress: float = 0.0     # 0.0-1.0
    status: str = GoalStatus.PENDING
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    result_note: str = ""     # 完成/失败时的一句话总结
    attempt_count: int = 0    # 尝试次数
    # V7 新增字段
    survival_gain: float = 0.0   # 对 survival 的贡献
    growth_gain: float = 0.0     # 对 growth 的贡献
    identity_gain: float = 0.0   # 对身份稳定的贡献
    source_drive: str = ""       # 来源驱动力名称(e.g. "curiosity_drive")

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_overdue(self) -> bool:
        if self.deadline_ticks <= 0:
            return False
        return self.elapsed_ticks >= self.deadline_ticks

    @property
    def urgency(self) -> float:
        """紧迫度：deadline 越近越高"""
        if self.deadline_ticks <= 0:
            return 0.5
        return min(1.0, self.elapsed_ticks / max(1, self.deadline_ticks))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "drive": self.drive,
            "description": self.description,
            "priority": self.priority,
            "deadline_ticks": self.deadline_ticks,
            "elapsed_ticks": self.elapsed_ticks,
            "progress": self.progress,
            "status": self.status,
            "created_at": self.created_at,
            "urgency": round(self.urgency, 2),
        }


class GoalSystem:
    """目标引擎——把驱动力变成行动。

    大脑的"我要做什么"模块。
    """

    def __init__(self, max_active: int = 3, default_deadline_ticks: int = 150):
        self.max_active = max_active
        self.default_deadline_ticks = default_deadline_ticks  # 约 5 分钟（2s/tick）
        self._goals: list[Goal] = []
        self._history: list[Goal] = []  # 已完成/失败的目标
        self._max_history = 50

        # 统计
        self.total_generated: int = 0
        self.total_completed: int = 0
        self.total_failed: int = 0
        self.last_generation_tick: int = 0

    # ── 目标生成 ──

    def generate_goals(
        self,
        drives: dict[str, float],
        recent_entities: list[str],
        curiosity_questions: list[dict],
        memory_count: int,
        current_tick: int,
    ) -> list[Goal]:
        """根据当前驱动力和上下文生成新目标。

        Args:
            drives: {drive_name: weight} — 来自 self_model
            recent_entities: 最近记忆中的关键词
            curiosity_questions: 好奇心引擎的待解问题
            memory_count: 记忆库总条数
            current_tick: 当前 tick 数

        Returns: 新生成的目标列表
        """
        # 活跃目标已达上限
        active_count = sum(1 for g in self._goals if g.status == GoalStatus.ACTIVE)
        if active_count >= self.max_active:
            return []

        new_goals = []
        quota = self.max_active - active_count

        # 按驱动力权重排序
        sorted_drives = sorted(drives.items(), key=lambda x: x[1], reverse=True)

        for drive_name, weight in sorted_drives[:3]:
            if len(new_goals) >= quota:
                break

            # 权重太低不生成目标
            if weight < 0.3:
                continue

            templates = DRIVE_GOAL_TEMPLATES.get(drive_name, [])
            if not templates:
                continue

            # 选择模板并填充
            template = templates[hash(str(current_tick) + drive_name) % len(templates)]

            # 填充实体
            entity = "未知领域"
            if recent_entities:
                idx = hash(str(current_tick)) % len(recent_entities)
                entity = recent_entities[idx]
            elif curiosity_questions:
                # 从好奇心问题中提取主题
                q = curiosity_questions[0].get("question", "")
                entity = q[:30]

            description = template.replace("{entity}", entity)

            # 计算优先级：驱动力权重 × 记忆库规模因子
            memory_factor = min(1.0, memory_count / 100.0)
            priority = weight * 0.7 + memory_factor * 0.3

            goal = Goal(
                id=f"goal-{uuid.uuid4().hex[:8]}",
                drive=drive_name,
                description=description,
                priority=round(priority, 2),
                deadline_ticks=self.default_deadline_ticks,
                status=GoalStatus.PENDING,
            )
            new_goals.append(goal)
            self._goals.append(goal)
            self.total_generated += 1

        # 好奇心问题升级：如果有 >5 个待解问题，生成一个"解答问题"目标
        if len(new_goals) < quota and len(curiosity_questions) >= 3:
            q = curiosity_questions[0]
            goal = Goal(
                id=f"goal-{uuid.uuid4().hex[:8]}",
                drive="curiosity",
                description=f"解答: {q.get('question', '未知问题')[:60]}",
                priority=0.5,
                deadline_ticks=self.default_deadline_ticks,
                status=GoalStatus.PENDING,
            )
            new_goals.append(goal)
            self._goals.append(goal)
            self.total_generated += 1

        if new_goals:
            self.last_generation_tick = current_tick
            logger.info(
                "goal-system: %d new goals generated (active=%d, total=%d)",
                len(new_goals), active_count + len(new_goals), self.total_generated,
            )

        return new_goals

    # ── Tick 推进 ──

    def tick_goals(self, current_tick: int) -> Goal | None:
        """每个 tick 推进活跃目标。

        返回: 如果需要立即执行的目标，返回它；否则 None
        """
        active = [g for g in self._goals if g.status == GoalStatus.ACTIVE]

        # 激活 pending 目标
        pending = [g for g in self._goals if g.status == GoalStatus.PENDING]
        if pending and len(active) < self.max_active:
            # 激活优先级最高的 pending 目标
            pending.sort(key=lambda g: g.priority, reverse=True)
            to_activate = pending[0]
            to_activate.status = GoalStatus.ACTIVE
            to_activate.started_at = datetime.now(timezone.utc).isoformat()
            active.append(to_activate)
            logger.info("goal-system: activated goal — %s", to_activate.description[:60])

        # 推进活跃目标
        for goal in active:
            goal.elapsed_ticks += 1

            # 超时检测
            if goal.is_overdue and goal.attempt_count == 0:
                goal.status = GoalStatus.FAILED
                goal.result_note = "超时未完成"
                goal.completed_at = datetime.now(timezone.utc).isoformat()
                self._archive(goal)
                self.total_failed += 1
                logger.info("goal-system: goal failed (timeout) — %s", goal.description[:60])
                continue

            # 逾期后给一次重试机会
            if goal.is_overdue and goal.attempt_count >= 1:
                goal.status = GoalStatus.FAILED
                goal.result_note = "重试后仍未完成"
                goal.completed_at = datetime.now(timezone.utc).isoformat()
                self._archive(goal)
                self.total_failed += 1
                continue

        # 返回优先级最高的活跃目标供执行
        if active:
            active.sort(key=lambda g: g.priority * (0.5 + 0.5 * g.urgency), reverse=True)
            return active[0]

        return None

    # ── 目标执行反馈 ──

    def mark_progress(self, goal_id: str, delta: float = 0.2):
        """标记目标进展。"""
        for g in self._goals:
            if g.id == goal_id:
                g.progress = min(1.0, g.progress + delta)
                if g.progress >= 0.95:
                    self._complete(g)
                return

    def mark_done(self, goal_id: str, note: str = ""):
        """手动标记目标完成。"""
        for g in self._goals:
            if g.id == goal_id and g.status == GoalStatus.ACTIVE:
                g.result_note = note
                self._complete(g)
                return

    def mark_failed(self, goal_id: str, note: str = ""):
        """手动标记目标失败。"""
        for g in self._goals:
            if g.id == goal_id and g.status == GoalStatus.ACTIVE:
                g.status = GoalStatus.FAILED
                g.result_note = note
                g.completed_at = datetime.now(timezone.utc).isoformat()
                self._archive(g)
                self.total_failed += 1
                return

    def _complete(self, goal: Goal):
        """内部：完成目标。"""
        goal.status = GoalStatus.DONE
        goal.progress = 1.0
        goal.completed_at = datetime.now(timezone.utc).isoformat()
        self._archive(goal)
        self.total_completed += 1
        logger.info("goal-system: goal completed — %s", goal.description[:60])

    def _archive(self, goal: Goal):
        """将目标移入历史。"""
        self._history.append(goal)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    # ── 查询 ──

    def get_active(self) -> list[Goal]:
        return [g for g in self._goals if g.status in (GoalStatus.ACTIVE, GoalStatus.PENDING)]

    def get_recently_completed(self, n: int = 5) -> list[Goal]:
        return [g for g in self._history if g.status == GoalStatus.DONE][-n:]

    def get_recently_failed(self, n: int = 5) -> list[Goal]:
        return [g for g in self._history if g.status == GoalStatus.FAILED][-n:]

    @property
    def active_count(self) -> int:
        return sum(1 for g in self._goals if g.status == GoalStatus.ACTIVE)

    @property
    def pending_count(self) -> int:
        return sum(1 for g in self._goals if g.status == GoalStatus.PENDING)

    # ── 快照 ──

    def snapshot(self) -> dict:
        return {
            "active_goals": [g.to_dict() for g in self.get_active()],
            "recent_completed": [
                {"description": g.description, "drive": g.drive, "completed_at": g.completed_at}
                for g in self.get_recently_completed(5)
            ],
            "recent_failed": [
                {"description": g.description, "drive": g.drive, "note": g.result_note}
                for g in self.get_recently_failed(3)
            ],
            "stats": {
                "active": self.active_count,
                "pending": self.pending_count,
                "total_generated": self.total_generated,
                "total_completed": self.total_completed,
                "total_failed": self.total_failed,
                "completion_rate": round(
                    self.total_completed / max(1, self.total_completed + self.total_failed), 2
                ),
            },
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "GoalSystem":
        gs = cls()
        if not data:
            return gs
        # 恢复活跃目标
        for gd in data.get("active_goals", []):
            goal = Goal(
                id=gd.get("id", ""),
                drive=gd.get("drive", "curiosity"),
                description=gd.get("description", ""),
                priority=gd.get("priority", 0.5),
                deadline_ticks=gd.get("deadline_ticks", 150),
                elapsed_ticks=gd.get("elapsed_ticks", 0),
                progress=gd.get("progress", 0.0),
                status=gd.get("status", GoalStatus.PENDING),
                created_at=gd.get("created_at", ""),
            )
            gs._goals.append(goal)
        gs.total_generated = data.get("stats", {}).get("total_generated", 0)
        gs.total_completed = data.get("stats", {}).get("total_completed", 0)
        gs.total_failed = data.get("stats", {}).get("total_failed", 0)
        return gs

    # ── 自我叙事反馈 ──

    def get_feedback_for_self_model(self) -> dict | None:
        """为目标完成/失败生成驱动力反馈。

        返回: {drive_name: delta} 或 None
        """
        feedback = {}
        recent = self._history[-3:]  # 最近 3 个结果
        if not recent:
            return None

        for goal in recent:
            if goal.status == GoalStatus.DONE:
                # 成功 → 该驱动力增强
                feedback[goal.drive] = feedback.get(goal.drive, 0) + 0.02
            elif goal.status == GoalStatus.FAILED:
                # 失败 → 该驱动力减弱（但不过度）
                feedback[goal.drive] = feedback.get(goal.drive, 0) - 0.01

        return feedback if feedback else None
