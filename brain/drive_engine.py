"""Drive Engine — V7 驱动力引擎。行为开始来源于内部需求。

V7 核心：驱动力不再是静态配置项，而是持续响应客观信号的动态需求系统。

七个驱动力：survival / curiosity / coherence / growth / exploration / creation / connection

每 tick 评估信号源 → 更新驱动力 → 写入 ActivationField → 影响目标生成。

设计原则:
  - 驱动力持续存在，不依赖用户输入
  - 信号驱动更新 + 缓慢衰减，防止无限累积
  - GoalGenerator 根据驱动力+身份+记忆生成具体目标
  - GoalScheduler 用加权评分自动排序
"""

import logging
from datetime import datetime, timezone
from typing import Any

from brain.goal_system import Goal, GoalStatus

logger = logging.getLogger("brain-v7.drive-engine")


# ══════════════════════════════════════════════
# 驱动力定义
# ══════════════════════════════════════════════

DRIVE_DEFINITIONS = {
    "survival_drive":   {"label": "生存",  "baseline": 0.5, "decay": 0.001},
    "curiosity_drive":  {"label": "好奇",  "baseline": 0.5, "decay": 0.001},
    "coherence_drive":  {"label": "一致",  "baseline": 0.6, "decay": 0.001},
    "growth_drive":     {"label": "成长",  "baseline": 0.5, "decay": 0.001},
    "exploration_drive":{"label": "探索",  "baseline": 0.4, "decay": 0.001},
    "creation_drive":   {"label": "创造",  "baseline": 0.3, "decay": 0.001},
    "connection_drive": {"label": "连接",  "baseline": 0.4, "decay": 0.001},
}


# ══════════════════════════════════════════════
# 信号源 → 驱动力映射
# ══════════════════════════════════════════════

SIGNAL_SOURCES: dict[str, dict[str, float]] = {
    "knowledge_gap": {      # 知识缺口（curiosity.open_questions > 3）
        "curiosity_drive":  +0.05,
        "growth_drive":     +0.03,
        "exploration_drive":+0.04,
    },
    "stagnation": {         # 长期停滞（total_ticks增长但completed不变）
        "growth_drive":     +0.08,
        "exploration_drive":+0.05,
        "creation_drive":   +0.03,
    },
    "identity_conflict": {  # 身份冲突（cingulate检测到记忆冲突）
        "survival_drive":   +0.06,
        "coherence_drive":  +0.08,
        "growth_drive":     +0.05,
    },
    "unknown_entity": {     # 未知内容（新实体不在exploration_topics中）
        "exploration_drive":+0.06,
        "curiosity_drive":  +0.04,
    },
    "success_feedback": {   # 成功反馈（metacognition.success_count上升）
        "creation_drive":   +0.04,
        "growth_drive":     -0.02,   # 成功→满足感，降低成长紧迫
        "connection_drive": +0.02,
    },
    "social_signal": {      # 社交信号（长时间无外部输入）
        "connection_drive": +0.05,
        "creation_drive":   +0.02,
    },
    "error_spike": {        # 错误率上升（llm_error_count骤增）
        "survival_drive":   +0.07,
        "coherence_drive":  +0.04,
    },
    "memory_decay": {       # 记忆衰减（decay归档数 > 0）
        "coherence_drive":  +0.03,
        "growth_drive":     +0.02,
    },
}

# 信号检测阈值
SIGNAL_THRESHOLDS = {
    "knowledge_gap_questions": 3,     # pending_count >= 3
    "stagnation_min_ticks": 100,      # ticks_since_last_completion >= 100
    "stagnation_min_total": 200,      # total_ticks >= 200 (避免启动时误判)
    "error_spike_threshold": 3,       # llm_error_count >= 3
    "social_silence_ticks": 60,       # ticks_since_input >= 60（~2分钟）
}


# ══════════════════════════════════════════════
# DriveEngine
# ══════════════════════════════════════════════

class DriveEngine:
    """驱动力引擎——持续评估信号源，更新驱动力，写入 ActivationField。

    不是一次性计算——每个 tick 累积信号影响。
    驱动力影响目标生成优先级（通过 ActivationField）。
    """

    def __init__(self):
        # 信号追踪（避免重复触发）
        self._last_signal_ticks: dict[str, int] = {}  # {signal_name: tick_when_emitted}
        self._signal_cooldown: int = 30  # 同一信号至少间隔30 tick才再次触发

        # 统计
        self.total_signals_emitted: int = 0
        self.total_goals_generated: int = 0
        self._ticks_since_last_completion: int = 0

    # ══════════════════════════════════════════════
    # 信号检测 → 驱动更新
    # ══════════════════════════════════════════════

    def tick(self, activation, brain_stem_state: dict, current_tick: int):
        """每个 tick 评估信号源并更新 ActivationField 中的驱动力维度。

        Args:
            activation: ActivationField 实例（读写驱动力维度）
            brain_stem_state: 来自 brain_stem 的状态快照
            current_tick: 当前 tick 数
        """
        signals = self._detect_signals(brain_stem_state, current_tick)

        if signals:
            self._apply_signals(activation, signals, current_tick)

        # 驱动力衰减（所有驱动力向 baseline 缓慢回归）
        self._decay_drives(activation)

    def _detect_signals(self, state: dict, tick: int) -> set[str]:
        """检测当前 tick 有哪些信号被触发。返回信号名集合。"""
        triggered = set()

        # 1. 知识缺口
        pending_qs = state.get("curiosity_pending", 0)
        if pending_qs >= SIGNAL_THRESHOLDS["knowledge_gap_questions"]:
            triggered.add("knowledge_gap")

        # 2. 长期停滞
        total_ticks = state.get("total_ticks", 0)
        goal_completed = state.get("goals_completed_recently", 0)
        if goal_completed == 0:
            self._ticks_since_last_completion += 1
        else:
            self._ticks_since_last_completion = 0

        if (self._ticks_since_last_completion >= SIGNAL_THRESHOLDS["stagnation_min_ticks"]
                and total_ticks >= SIGNAL_THRESHOLDS["stagnation_min_total"]):
            triggered.add("stagnation")

        # 3. 身份冲突
        conflict_detected = state.get("conflict_detected", False)
        if conflict_detected:
            triggered.add("identity_conflict")

        # 4. 未知内容
        unknown_count = state.get("unknown_entities_count", 0)
        if unknown_count > 0:
            triggered.add("unknown_entity")

        # 5. 成功反馈
        recent_successes = state.get("recent_successes", 0)
        if recent_successes >= 2:
            triggered.add("success_feedback")

        # 6. 社交信号
        ticks_since_input = state.get("ticks_since_input", 0)
        if ticks_since_input >= SIGNAL_THRESHOLDS["social_silence_ticks"]:
            triggered.add("social_signal")

        # 7. 错误率上升
        llm_errors = state.get("llm_error_count", 0)
        if llm_errors >= SIGNAL_THRESHOLDS["error_spike_threshold"]:
            triggered.add("error_spike")

        # 8. 记忆衰减
        memories_archived = state.get("memories_archived", 0)
        if memories_archived > 0:
            triggered.add("memory_decay")

        return triggered

    def _apply_signals(self, activation, signals: set[str], tick: int):
        """将信号映射为驱动力增量，写入 ActivationField。"""
        for signal_name in signals:
            # 冷却检查
            last_tick = self._last_signal_ticks.get(signal_name, -999)
            if tick - last_tick < self._signal_cooldown:
                continue

            deltas = SIGNAL_SOURCES.get(signal_name, {})
            for drive_name, delta in deltas.items():
                activation.set(drive_name,
                    min(1.0, max(0.0, activation.get(drive_name) + delta)))

            self._last_signal_ticks[signal_name] = tick
            self.total_signals_emitted += 1
            logger.debug("drive-engine: signal '%s' → %s", signal_name,
                         {k: round(v, 3) for k, v in deltas.items()})

    def _decay_drives(self, activation):
        """所有驱动力向 baseline 缓慢回归（每 tick 0.1%）。"""
        for name, defn in DRIVE_DEFINITIONS.items():
            cur = activation.get(name)
            target = defn["baseline"]
            if abs(cur - target) > 0.001:
                activation.set(name, cur + (target - cur) * defn["decay"])

    def notify_goal_completed(self):
        """目标完成时重置停滞计数器。"""
        self._ticks_since_last_completion = 0

    def snapshot(self) -> dict:
        return {
            "total_signals_emitted": self.total_signals_emitted,
            "total_goals_generated": self.total_goals_generated,
            "ticks_since_completion": self._ticks_since_last_completion,
            "last_signals": {k: v for k, v in self._last_signal_ticks.items()
                            if v > 0},
        }


# ══════════════════════════════════════════════
# GoalGenerator — 驱动力 → 目标
# ══════════════════════════════════════════════

import uuid as _uuid

DRIVE_TO_GOAL_TYPE: dict[str, str] = {
    "survival_drive":   "self_preservation",
    "curiosity_drive":  "curiosity",
    "coherence_drive":  "coherence",
    "growth_drive":     "growth",
    "exploration_drive":"curiosity",  # 探索本质是好奇心驱动
    "creation_drive":   "growth",     # 创造本质是成长驱动
    "connection_drive": "connection",
}


class GoalGenerator:
    """根据驱动力、身份、记忆生成具体目标。"""

    def generate(
        self,
        activation,
        identity_traits: list[str],
        recent_entities: list[str],
        curiosity_questions: list[dict],
        memory_count: int,
        current_tick: int,
    ) -> list[Goal]:
        """生成目标。按最强驱动力分配配额。

        返回: 新目标列表。
        """
        # 找到最强的 3 个驱动力
        drives_sorted = sorted(
            [(name, activation.get(name)) for name in DRIVE_DEFINITIONS],
            key=lambda x: x[1], reverse=True,
        )

        new_goals = []
        entities_pool = list(recent_entities[:8])
        if curiosity_questions:
            entities_pool.extend([q.get("question", "")[:20] for q in curiosity_questions[:3]])

        for drive_name, intensity in drives_sorted[:3]:
            if intensity < 0.4:  # 驱动力太弱，不生成目标
                continue

            entity = entities_pool[hash(str(current_tick) + drive_name) % len(entities_pool)] if entities_pool else "未知领域"
            drive_type = DRIVE_TO_GOAL_TYPE.get(drive_name, "growth")

            # 根据驱动力类型生成目标描述
            description = self._describe(drive_name, entity)

            # 计算收益
            gains = self._compute_gains(drive_name)

            goal = Goal(
                id=f"goal-{_uuid.uuid4().hex[:8]}",
                drive=drive_type,
                description=description,
                priority=round(intensity * 0.8, 2),
                deadline_ticks=150,
                status=GoalStatus.PENDING,
                survival_gain=gains.get("survival", 0.0),
                growth_gain=gains.get("growth", 0.0),
                identity_gain=gains.get("identity", 0.0),
                source_drive=drive_name,
            )
            new_goals.append(goal)

        return new_goals

    def _describe(self, drive_name: str, entity: str) -> str:
        """根据驱动力生成目标描述。"""
        templates = {
            "survival_drive":   f"检查并维护系统稳定性：{entity}",
            "curiosity_drive":  f"学习关于 {entity} 的更多信息",
            "coherence_drive":  f"验证关于 {entity} 的认知是否一致",
            "growth_drive":     f"掌握 {entity} 并将其应用到实践中",
            "exploration_drive":f"探索 {entity} 相关的未知领域",
            "creation_drive":   f"基于 {entity} 创造新的内容或方法",
            "connection_drive": f"了解调用者关于 {entity} 的需求",
        }
        return templates.get(drive_name, f"推进 {entity}")

    def _compute_gains(self, drive_name: str) -> dict[str, float]:
        """计算目标对各维度的收益。"""
        gain_map = {
            "survival_drive":   {"survival": 0.8, "growth": 0.1, "identity": 0.2},
            "curiosity_drive":  {"survival": 0.0, "growth": 0.6, "identity": 0.3},
            "coherence_drive":  {"survival": 0.2, "growth": 0.5, "identity": 0.7},
            "growth_drive":     {"survival": 0.1, "growth": 0.8, "identity": 0.4},
            "exploration_drive":{"survival": 0.0, "growth": 0.5, "identity": 0.2},
            "creation_drive":   {"survival": 0.0, "growth": 0.7, "identity": 0.5},
            "connection_drive": {"survival": 0.1, "growth": 0.3, "identity": 0.4},
        }
        return gain_map.get(drive_name, {"survival": 0.1, "growth": 0.3, "identity": 0.2})


# ══════════════════════════════════════════════
# GoalScheduler — 目标优先级排序
# ══════════════════════════════════════════════

class GoalScheduler:
    """对活跃目标评分排序，选择最高分目标执行。"""

    def score(self, goal: Goal, activation) -> float:
        """计算目标的综合评分。

        评分 = priority×0.20 + urgency×arousal×0.15
             + survival_gain×survival_drive×0.20
             + growth_gain×growth_drive×0.20
             + identity_gain×(1-identity_stability)×0.15
             + progress×(-0.10)
        """
        arousal = activation.get("arousal")
        survival_d = activation.get("survival_drive")
        growth_d = activation.get("growth_drive")
        identity_stab = activation.get("identity_stability")

        return (
            goal.priority * 0.20
            + goal.urgency * arousal * 0.15
            + goal.survival_gain * survival_d * 0.20
            + goal.growth_gain * growth_d * 0.20
            + goal.identity_gain * (1.0 - identity_stab) * 0.15
            + goal.progress * (-0.10)
        )

    def schedule(self, goals: list[Goal], activation, max_active: int = 3) -> list[Goal]:
        """排序并返回应激活的目标列表。

        自动取消低分目标（如果超过最大活跃数）。
        """
        if not goals:
            return []

        # 评分排序
        scored = [(g, self.score(g, activation)) for g in goals]
        scored.sort(key=lambda x: x[1], reverse=True)

        # 标记前 N 个为 active
        active = []
        for i, (goal, score) in enumerate(scored):
            if i < max_active:
                if goal.status == GoalStatus.PENDING:
                    goal.status = GoalStatus.ACTIVE
                    goal.started_at = datetime.now(timezone.utc).isoformat()
                active.append(goal)
            else:
                # 自动取消低分目标
                if goal.status in (GoalStatus.PENDING, GoalStatus.ACTIVE):
                    goal.status = GoalStatus.ABANDONED
                    goal.result_note = f"优先级不足(score={score:.3f})，自动取消"
                    goal.completed_at = datetime.now(timezone.utc).isoformat()
                    logger.debug("goal-scheduler: abandoned goal '%s' (score=%.3f)",
                                 goal.description[:40], score)

        return active


# ══════════════════════════════════════════════
# 便捷构建
# ══════════════════════════════════════════════

def build_state_snapshot_for_drive_engine(brain_stem) -> dict:
    """从 brain_stem 构建 DriveEngine 需要的状态快照。

    避免 DriveEngine 直接依赖 brain_stem 的内部结构。
    """
    return {
        "total_ticks": brain_stem.state.total_ticks,
        "ticks_since_input": brain_stem.state.ticks_since_input,
        "conflict_detected": brain_stem.state.conflict_detected,
        "llm_error_count": brain_stem.state.llm_error_count,
        "curiosity_pending": brain_stem.state.curiosity.pending_count,
        "goals_completed_recently": brain_stem.goal_system.total_completed,
        "recent_successes": brain_stem.metacognition.success_count,
        "unknown_entities_count": len(brain_stem.state.curiosity.exploration_topics),
        "memories_archived": getattr(brain_stem, '_last_archived_count', 0),
    }
