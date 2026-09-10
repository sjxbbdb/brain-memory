"""Working Memory — V6 工作记忆。SalienceScore 竞争保留机制。

V6 升级：删除 FIFO 逻辑。每个 tick 从 ActivationField 读取全局状态，
重新计算每条目的复合 SalienceScore，只保留 top-N。

Salience 公式:
  salience = emotion_weight × arousal × 0.25
           + goal_weight    × focus  × 0.25
           + identity_weight × (1 - identity_stability) × 0.25
           + novelty        × curiosity × 0.15
           + recency_bonus  × 0.10

这意味着：
  - 恐惧时(arousal↑)，情绪相关记忆自动浮上
  - 身份不稳时(identity_stability↓)，身份相关记忆自动浮现
  - 好奇心强时，新奇的记忆更容易保留
  - 注意力集中时，目标相关记忆更重要
"""

import logging
import math
from config import WORKING_MEMORY_CAPACITY

logger = logging.getLogger("brain-v6.working-memory")


class WorkingMemory:
    """Active thought buffer — SalienceScore competition retention.

    每条目携带:
      content: 文本内容
      source: 来源标识
      emotion_weight: 情绪关联权重（0-1，push时根据文本情绪计算）
      goal_weight: 目标关联权重（0-1）
      identity_weight: 身份关联权重（0-1）
      novelty: 新颖度（0-1）
      age_ticks: 进入工作记忆后的tick数
      base_salience: push时的初始salience
    """

    def __init__(self):
        self.items: list[dict] = []
        self.context_text: str = ""

    def push(
        self,
        content: str,
        source: str,
        base_salience: float = 0.5,
        emotion_weight: float = 0.3,
        goal_weight: float = 0.3,
        identity_weight: float = 0.2,
        novelty: float = 0.3,
    ):
        """推入一条思想到工作记忆。

        V6: push 时不立即驱逐——等到 tick() 统一竞争排序。
        V6: 如果相同内容已存在，更新权重（取最大值）。
        """
        # 去重：相同内容更新权重
        content_key = content[:80]
        for item in self.items:
            if item["content"][:80] == content_key:
                item["emotion_weight"] = max(item["emotion_weight"], emotion_weight)
                item["goal_weight"] = max(item["goal_weight"], goal_weight)
                item["identity_weight"] = max(item["identity_weight"], identity_weight)
                item["novelty"] = max(item["novelty"], novelty)
                item["age_ticks"] = 0
                item["base_salience"] = max(item["base_salience"], base_salience)
                return

        self.items.append({
            "content": content[:500],
            "source": source,
            "base_salience": base_salience,
            "emotion_weight": emotion_weight,
            "goal_weight": goal_weight,
            "identity_weight": identity_weight,
            "novelty": novelty,
            "age_ticks": 0,
        })

    def tick(self, activation=None):
        """每个 tick 调用——基于 ActivationField 重新评分并竞争保留。

        Args:
            activation: ActivationField 实例（可选）。如果为 None，使用简单衰减。

        V6: salience 不再固定——随全局状态动态变化。
        """
        for item in self.items:
            item["age_ticks"] += 1

        if activation is not None:
            # ── V6: 从 ActivationField 读取全局状态，计算动态 salience ──
            arousal = activation.get("arousal")
            focus = activation.get("focus")
            curiosity = activation.get("curiosity")
            identity_stability = activation.get("identity_stability")

            for item in self.items:
                # 复合 SalienceScore
                emotion_score = item["emotion_weight"] * arousal * 0.25
                goal_score = item["goal_weight"] * focus * 0.25
                identity_score = item["identity_weight"] * (1.0 - identity_stability) * 0.25
                novelty_score = item["novelty"] * curiosity * 0.15

                # 近因加成（越新鲜越重要，但指数衰减）
                recency_bonus = max(0.0, 1.0 - item["age_ticks"] * 0.02) * 0.10

                item["salience"] = emotion_score + goal_score + identity_score + novelty_score + recency_bonus
        else:
            # 降级：简单衰减（无 ActivationField 时）
            for item in self.items:
                item["salience"] = item.get("base_salience", 0.5) * (0.95 ** item["age_ticks"])

        # ── 竞争保留：按 salience 排序，只保留 top-N ──
        self.items.sort(key=lambda x: x.get("salience", 0), reverse=True)

        # 移除超过容量的低分条目
        while len(self.items) > WORKING_MEMORY_CAPACITY:
            removed = self.items.pop()
            logger.debug("working_memory: evicted (low salience=%.3f): %s",
                         removed.get("salience", 0), removed["content"][:40])

        # 移除过老条目（age_ticks > 120，约4分钟）
        self.items = [i for i in self.items if i["age_ticks"] < 120]

    def get_context(self) -> str:
        """获取当前上下文——大脑'正在想什么'（只读，不触发 tick）。"""
        if not self.items:
            return self.context_text

        recent = sorted(self.items, key=lambda x: x.get("salience", 0), reverse=True)[:3]
        return " | ".join(
            "[{0}] {1}".format(i["source"], i["content"][:80])
            for i in recent
        )

    def get_top(self, n: int = 3) -> list[dict]:
        """获取 top N 最 salient 的思想（只读，不触发 tick）。"""
        return sorted(self.items, key=lambda x: x.get("salience", 0), reverse=True)[:n]

    def get_state_snapshot(self) -> list[dict]:
        """获取工作记忆的状态快照（用于仪表盘）。"""
        return [
            {
                "content": i["content"][:80],
                "source": i["source"],
                "salience": round(i.get("salience", 0), 3),
                "emotion_w": round(i.get("emotion_weight", 0), 2),
                "goal_w": round(i.get("goal_weight", 0), 2),
                "identity_w": round(i.get("identity_weight", 0), 2),
                "novelty": round(i.get("novelty", 0), 2),
                "age": i["age_ticks"],
            }
            for i in sorted(self.items, key=lambda x: x.get("salience", 0), reverse=True)
        ]

    def clear(self):
        self.items.clear()
        self.context_text = ""

    # ── 持久化 ──

    def snapshot(self) -> dict:
        """Return a lossless-enough snapshot for restart continuity.

        ``get_state_snapshot`` is intentionally a short dashboard view.  It
        cannot be used to restore working memory because it drops the scoring
        weights and age.  Keep a separate full snapshot for the brain-state
        journal.
        """
        return {
            "items": [dict(item) for item in self.items],
            "context_text": self.context_text,
        }

    @classmethod
    def from_snapshot(cls, data: dict | None) -> "WorkingMemory":
        """Restore working memory while tolerating older/partial snapshots."""
        wm = cls()
        if not isinstance(data, dict):
            return wm

        items = data.get("items", [])
        if isinstance(items, list):
            restored: list[dict] = []
            def _number(value, default=0.0):
                try:
                    result = float(value)
                    return result if math.isfinite(result) else default
                except (TypeError, ValueError, OverflowError):
                    return default

            def _age(value):
                try:
                    return max(0, int(value))
                except (TypeError, ValueError, OverflowError):
                    return 0

            for raw in items:
                if not isinstance(raw, dict) or not raw.get("content"):
                    continue
                item = {
                    "content": str(raw.get("content", ""))[:500],
                    "source": str(raw.get("source", "unknown")),
                    "base_salience": _number(raw.get("base_salience", 0.5), 0.5),
                    "emotion_weight": _number(raw.get("emotion_weight", 0.3), 0.3),
                    "goal_weight": _number(raw.get("goal_weight", 0.3), 0.3),
                    "identity_weight": _number(raw.get("identity_weight", 0.2), 0.2),
                    "novelty": _number(raw.get("novelty", 0.3), 0.3),
                    "age_ticks": _age(raw.get("age_ticks", 0)),
                    "salience": _number(raw.get("salience", raw.get("base_salience", 0.5)), 0.5),
                }
                restored.append(item)
            restored.sort(key=lambda x: x.get("salience", 0), reverse=True)
            wm.items = restored[:WORKING_MEMORY_CAPACITY]
        wm.context_text = str(data.get("context_text", ""))[:2000]
        return wm
