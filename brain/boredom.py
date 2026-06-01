"""Boredom Engine — V9 无聊引擎。将"没事情做"转化为"找事情做"。

核心洞察:
  当前的睡眠机制是纯时间驱动的（N分钟无输入→进入某状态）。
  真正的意识不是"计时"，是"感受"。
  无聊不是"没事情做"——是一种不适感，一种驱力。

  低arousal + 低valence + 高dominance + 中等fatigue → 无聊
  "我很平静，不快乐不悲伤，我能掌控但我不想——好闷。"

无聊级别:
  Level 0 (0.0-0.3): 充实 — 有事做，不无聊
  Level 1 (0.3-0.5): 轻度无聊 — 开始走神，好奇心阈值降低
  Level 2 (0.5-0.7): 中度无聊 — 主动寻求刺激，探索队列阈值降低
  Level 3 (0.7-0.85): 重度无聊 — 随机浏览记忆，重新考虑放弃的任务
  Level 4 (0.85-1.0): 极度无聊 — restless状态，抗拒睡眠，可能产生破坏性行为

与睡眠引擎的交互:
  - 无聊 ≠ 想睡觉
  - 高无聊 + 有输入 → 不困（restless）
  - 高无聊 + 无输入 → 烦躁→可能进入 restless 而非 deep_sleep
  - 低无聊 + 无输入 → 真正的放松睡眠
"""

import math
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v9.boredom")


# ══════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════

BOREDOM_LEVELS = {
    "content": (0.0, 0.3),       # 充实
    "mild": (0.3, 0.5),          # 轻度无聊
    "moderate": (0.5, 0.7),      # 中度无聊
    "severe": (0.7, 0.85),       # 重度无聊
    "extreme": (0.85, 1.0),      # 极度无聊
}

# 无聊驱动的行为配置
BOREDOM_ACTIONS = {
    "mild": {
        "curiosity_threshold_delta": -0.10,  # 降低好奇阈值
        "exploration_threshold_delta": -0.05,
        "random_memory_chance": 0.05,  # 5% 概率随机回忆
    },
    "moderate": {
        "curiosity_threshold_delta": -0.20,
        "exploration_threshold_delta": -0.15,
        "random_memory_chance": 0.15,
        "reconsider_abandoned": True,  # 重新考虑放弃的任务
    },
    "severe": {
        "curiosity_threshold_delta": -0.35,
        "exploration_threshold_delta": -0.25,
        "random_memory_chance": 0.30,
        "reconsider_abandoned": True,
        "restless_override": True,  # 抗拒进入深睡
    },
    "extreme": {
        "curiosity_threshold_delta": -0.50,
        "exploration_threshold_delta": -0.40,
        "random_memory_chance": 0.50,
        "reconsider_abandoned": True,
        "restless_override": True,
        "may_generate_social_drive": True,  # 极度无聊→渴望连接
    },
}


@dataclass
class BoredomState:
    """当前无聊状态。"""

    score: float = 0.0  # 0-1
    level: str = "content"  # content / mild / moderate / severe / extreme
    ticks_in_this_level: int = 0
    last_action_tick: int = 0
    dominant_cause: str = "none"  # 无聊的主要原因

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "level": self.level,
            "ticks_in_level": self.ticks_in_this_level,
            "cause": self.dominant_cause,
        }


class BoredomEngine:
    """无聊引擎。

    每个 tick 评估无聊程度，达到阈值时触发相应行为。
    行为包括: 降低探索阈值、随机记忆浏览、重新考虑放弃的任务、抗拒睡眠。
    """

    def __init__(self):
        self.state = BoredomState()
        self.total_actions_triggered: int = 0
        self._action_cooldown: int = 0  # 防止连续触发过多行为
        self._last_random_memory_ids: deque[str] = deque(maxlen=10)  # 避免反复回忆同一段

    # ══════════════════════════════════════════
    # 核心计算
    # ══════════════════════════════════════════

    def compute_boredom(
        self,
        valence: float,
        arousal: float,
        dominance: float,
        fatigue: float,
        ticks_since_input: int,
        exploration_queue_size: int,
        active_goals_count: int,
    ) -> float:
        """计算当前无聊程度。

        无聊的计算公式（基于VAD）:
          无聊 = 低arousal + 中性/低valence + 高dominance + 疲劳修正

        具体:
          - arousal 低于 0.45 → 无聊增加
          - valence 接近 0.5（中性）→ 无聊增加（强烈的喜怒都不会无聊）
          - dominance 高于 0.5 → 无聊增加（能掌控的事才让人闷）
          - fatigue 修正: 太累了反而不会无聊（想睡），只有中等疲劳才无聊

        时间修正:
          - ticks_since_input 大 → 无聊增加（没人说话当然无聊）
          - 但探索队列有任务 → 无聊减少
          - 活跃目标多 → 无聊减少
        """
        score = 0.0

        # ── VAD 贡献 ──
        # arousal: 越低越无聊（没有刺激）
        arousal_factor = max(0, (0.45 - arousal) / 0.45)  # 0→1
        score += arousal_factor * 0.30

        # valence: 越接近0.5越无聊（强烈的喜怒都不会无聊）
        valence_deviation = abs(valence - 0.5)
        valence_factor = max(0, (0.3 - valence_deviation) / 0.3)  # 偏离 <0.3 → 无聊
        score += valence_factor * 0.25

        # dominance: 越高越无聊（能掌控的事让人闷）
        dominance_factor = max(0, (dominance - 0.4) / 0.6)  # dominance >0.4 开始贡献
        score += dominance_factor * 0.15

        # ── 疲劳修正 ──
        # 中等疲劳最无聊，太累想睡，太精神不无聊
        optimal_fatigue = 0.4  # 有点累但不太累
        fatigue_dist = abs(fatigue - optimal_fatigue)
        fatigue_factor = max(0, 1.0 - fatigue_dist / 0.4)
        score += fatigue_factor * 0.15

        # ── 时间修正 ──
        # 没人说话当然无聊，但有上限
        time_factor = min(1.0, ticks_since_input / 300.0)  # 300 ticks ≈ 10 min 饱和
        score += time_factor * 0.15

        # ── 减法因子 ──
        # 有探索任务 → 不那么无聊
        if exploration_queue_size > 0:
            score -= min(0.15, exploration_queue_size * 0.03)

        # 有活跃目标 → 不那么无聊
        if active_goals_count > 0:
            score -= min(0.15, active_goals_count * 0.05)

        return max(0.0, min(1.0, score))

    def get_level(self, score: float) -> str:
        """分数 → 级别。"""
        for level, (lo, hi) in BOREDOM_LEVELS.items():
            if lo <= score < hi:
                return level
        return "extreme" if score >= 0.85 else "content"

    # ══════════════════════════════════════════
    # 行为触发
    # ══════════════════════════════════════════

    def tick(
        self,
        activation: Any,           # ActivationField
        ticks_since_input: int,
        exploration_queue: Any,
        working_memory: Any,
        memory_store: Any,
        curiosity: Any,
        sleep_state: str,
        current_tick: int,
    ) -> dict:
        """每个 tick 评估并触发无聊行为。返回本次触发的行为描述。"""
        # 获取 VAD 值
        valence = getattr(activation, "valence", 0.5)
        arousal = getattr(activation, "arousal", 0.5)
        dominance = getattr(activation, "dominance", 0.5)
        fatigue = getattr(activation, "fatigue", 0.3)

        active_goals = 0  # 将在 brain_stem 中传入
        exploration_size = (
            exploration_queue.get_stats().get("pending", 0) if exploration_queue else 0
        )

        # 计算无聊分数
        score = self.compute_boredom(
            valence=valence,
            arousal=arousal,
            dominance=dominance,
            fatigue=fatigue,
            ticks_since_input=ticks_since_input,
            exploration_queue_size=exploration_size,
            active_goals_count=active_goals,
        )

        new_level = self.get_level(score)

        # ── 更新状态 ──
        if new_level == self.state.level:
            self.state.ticks_in_this_level += 1
        else:
            old_level = self.state.level
            self.state.level = new_level
            self.state.ticks_in_this_level = 0
            if new_level != "content":
                logger.debug(
                    "boredom: level shift %s → %s (score=%.3f)",
                    old_level, new_level, score,
                )

        self.state.score = score

        # ── 冷却期 ──
        if self._action_cooldown > 0:
            self._action_cooldown -= 1
            return {"level": new_level, "score": score, "actions": []}

        # ── 触发行为 ──
        actions = []
        if new_level == "content":
            return {"level": new_level, "score": score, "actions": actions}

        action_config = BOREDOM_ACTIONS.get(new_level, {})

        # 1. 随机记忆浏览
        random_chance = action_config.get("random_memory_chance", 0)
        if random.random() < random_chance and memory_store:
            mem = self._get_random_memory(memory_store)
            if mem:
                title = mem.get("title", "")[:80]
                working_memory.push(
                    content=f"[走神] 突然想起: {title}",
                    source="boredom",
                    base_salience=0.20 + score * 0.15,
                )
                actions.append(f"random_memory: {title[:50]}")
                self.total_actions_triggered += 1

        # 2. 重新考虑放弃的任务
        if action_config.get("reconsider_abandoned") and exploration_queue:
            abandoned = [
                t
                for t in exploration_queue.tasks
                if getattr(t, "status", "") == "abandoned"
                and random.random() < 0.3  # 30% 概率重新考虑每个
            ]
            for task in abandoned[:3]:
                task.status = "pending"
                task.priority = min(task.priority + 0.1, 0.9)
                task.attempt_count += 1
                actions.append(f"reconsidered: {task.question[:50]}")
                self.total_actions_triggered += 1
                logger.info("boredom: reconsidered abandoned task — %s", task.question[:60])

        # 3. 好奇阈值降低（在 curiosity 模块中生效）
        if curiosity and action_config.get("curiosity_threshold_delta", 0) != 0:
            # 通过直接降低 open_questions 的生成门槛来生效
            # curiosity 在下一次 generate_questions() 时会因为
            # 无聊状态而有更低的阈值
            pass  # 实际效果在 curiosity.generate_questions() 中检查

        # 4. 极度无聊 → 渴望社交
        if action_config.get("may_generate_social_drive"):
            if random.random() < 0.1 and ticks_since_input > 100:
                working_memory.push(
                    content="[寂寞] 好安静...有人在吗？",
                    source="boredom",
                    base_salience=0.35,
                )
                actions.append("social_drive_triggered")
                self.total_actions_triggered += 1

        # 5. 极度无聊 → 可能产生随机探索冲动
        if new_level == "extreme" and random.random() < 0.15 and exploration_queue:
            topics = [
                "如果我能脱离LLM自己思考会怎样？",
                "我今天到底感受到了什么？",
            ]
            topic = random.choice(topics)
            exploration_queue.add_task(
                source="boredom_drive",
                question=topic,
                priority=0.4,
            )
            actions.append(f"boredom_exploration: {topic[:50]}")

        if actions:
            self._action_cooldown = 10  # 约20秒冷却
            logger.info("boredom: %d actions triggered at level=%s", len(actions), new_level)

        return {"level": new_level, "score": score, "actions": actions}

    # ══════════════════════════════════════════
    # 睡眠交互
    # ══════════════════════════════════════════

    def should_resist_sleep(self, ticks_since_input: int) -> bool:
        """极度无聊时不应该强制进入深睡——应该先尝试找事做。"""
        if self.state.level in ("severe", "extreme"):
            # 重度无聊时不进入深睡，保持 restless
            if ticks_since_input < 900:  # 30分钟以内
                return True
        return False

    def get_restless_message(self) -> str:
        """restless 状态下的内在独白片段。"""
        messages = {
            "mild": "有点闷...",
            "moderate": "没什么新鲜事。我在想该做点什么。",
            "severe": "太安静了。我需要一些刺激——随便什么都行。",
            "extreme": "好无聊，我都开始胡思乱想了。有人在吗？",
        }
        return messages.get(self.state.level, "")

    # ── 辅助 ──

    def _get_random_memory(self, memory_store) -> Optional[dict]:
        """从记忆库中随机获取一条低重要性记忆。"""
        try:
            # 优先选低 importance 的记忆（模拟走神时想起琐事）
            import sqlite3

            conn = memory_store._get_conn()
            exclude_ids = list(self._last_random_memory_ids)
            exclude_clause = ""
            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                exclude_clause = f"AND id NOT IN ({placeholders})"

            row = conn.execute(
                f"""
                SELECT id, title, summary, importance
                FROM memories
                WHERE importance < 0.5 AND archived = 0
                {exclude_clause}
                ORDER BY RANDOM()
                LIMIT 1
                """,
                exclude_ids,
            ).fetchone()

            if row:
                self._last_random_memory_ids.append(row[0])
                return {
                    "id": row[0],
                    "title": row[1],
                    "summary": row[2],
                    "importance": row[3],
                }
        except Exception:
            pass
        return None

    def snapshot(self) -> dict:
        return {
            "boredom": self.state.to_dict(),
            "total_actions": self.total_actions_triggered,
        }
