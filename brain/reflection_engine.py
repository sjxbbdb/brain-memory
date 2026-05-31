"""ReflectionEngine — V8 周期性反思引擎。

周期运行（每 deep_reflection_interval）。检查：
  - 目标是否有效
  - 结论是否正确
  - 身份是否变化
  - 长期方向是否偏移

形成 ReflectionMemory（type=reflection 的 episodic memory）。
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v8.reflection-engine")


class ReflectionEngine:
    """周期性反思——大脑的"我做得对吗"模块。

    不同于 _deep_self_reflection（身份层面的思考），
    这里检查的是执行层面：目标进展、结论质量、方向一致性。
    """

    def __init__(self):
        self.total_reflections: int = 0
        self.last_reflection_tick: int = 0

        # 追踪
        self._invalid_goals_cancelled: int = 0
        self._wrong_conclusions_marked: int = 0

    def reflect(
        self,
        brain_stem,
        memory_store,
        current_tick: int,
    ) -> dict | None:
        """执行一次反思。

        返回: ReflectionMemory dict（用于存储到记忆库），或 None。
        """
        self.total_reflections += 1
        self.last_reflection_tick = current_tick

        findings = []

        # ── 1. 检查目标有效性 ──
        gs = brain_stem.goal_system
        for goal in gs.get_active():
            # 超时太久的放弃
            if goal.is_overdue and goal.attempt_count >= 1:
                gs.mark_failed(goal.id, "反思：多次超时，目标可能不可行")
                self._invalid_goals_cancelled += 1
                findings.append(f"取消无效目标: {goal.description[:40]}")
            # 优先级太低且无进展
            elif goal.priority < 0.2 and goal.progress < 0.1 and goal.elapsed_ticks > 100:
                gs.mark_failed(goal.id, "反思：优先级过低且无进展")
                self._invalid_goals_cancelled += 1
                findings.append(f"取消低优先级目标: {goal.description[:40]}")

        # ── 2. 检查结论正确性（探索任务） ──
        if hasattr(brain_stem, 'exploration_queue'):
            eq = brain_stem.exploration_queue
            for task in eq.tasks:
                if task.status == "resolved" and task.conclusion:
                    # 简单检查：结论太短可能不可靠
                    if len(task.conclusion) < 10:
                        task.status = "abandoned"
                        task.conclusion = "反思：结论不充分"
                        self._wrong_conclusions_marked += 1
                        findings.append(f"标记不充分结论: {task.question[:40]}")

        # ── 3. 检查身份变化 ──
        sm = brain_stem.state.self_model
        old_version = sm.identity_version
        if old_version != getattr(self, '_last_identity_version', old_version):
            findings.append(f"身份已从 v{getattr(self, '_last_identity_version', old_version)} 演化到 v{old_version}")
        self._last_identity_version = old_version

        # ── 4. 检查长期方向 ──
        # CorePurpose 对齐检查
        from brain.core_purpose import core_purpose as cp
        active_goals = gs.get_active()
        if active_goals:
            avg_alignment = sum(cp.align_score(g) for g in active_goals) / len(active_goals)
            if avg_alignment < 0.3:
                findings.append(f"当前目标与 CorePurpose 对齐度低({avg_alignment:.2f})")

        if not findings:
            return None

        # ── 形成 ReflectionMemory ──
        reflection_text = "反思检查结果: " + "; ".join(findings)
        reflection_memory = {
            "type": "reflection",
            "title": "周期性反思",
            "content": reflection_text[:500],
            "summary": reflection_text[:200],
            "importance": 0.6,
            "entities": ["反思", "目标审计", "身份检查"],
            "emotion_tags": [],
            "source": "reflection_engine",
            "emotion_label": "neutral",
            "created": datetime.now(timezone.utc).isoformat(),
        }

        if memory_store:
            mem_id = memory_store.save(reflection_memory)
            reflection_memory["id"] = mem_id
            logger.info("reflection-engine: ReflectionMemory saved (id=%s)", mem_id)

        logger.info("reflection-engine: %d findings at tick %d", len(findings), current_tick)
        return reflection_memory

    def snapshot(self) -> dict:
        return {
            "total_reflections": self.total_reflections,
            "invalid_goals_cancelled": self._invalid_goals_cancelled,
            "wrong_conclusions_marked": self._wrong_conclusions_marked,
        }
