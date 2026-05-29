"""Working Memory — 工作记忆。活跃思考缓冲区（规则引擎）。

职责:
  1. 维护当前"在想什么"的活跃条目列表
  2. 容量上限 WORKING_MEMORY_CAPACITY (7条)
  3. 旧条目自动衰减、轮换
"""

import logging
from config import WORKING_MEMORY_CAPACITY

logger = logging.getLogger("brain-v4.working-memory")


class WorkingMemory:
    """Active thought buffer — what the brain is thinking about RIGHT NOW."""

    def __init__(self):
        self.items: list[dict] = []       # [{"content": str, "source": str, "age_ticks": int, "salience": float}, ...]
        self.context_text: str = ""

    def push(self, content: str, source: str, salience: float = 0.5):
        """Push a thought into working memory. Oldest/lowest-salience items get evicted."""
        # Don't store duplicates
        for item in self.items:
            if item["content"][:80] == content[:80]:
                item["age_ticks"] = 0
                item["salience"] = salience
                return

        self.items.insert(0, {
            "content": content[:500],
            "source": source,
            "age_ticks": 0,
            "salience": salience,
        })

        # Evict overflow — remove lowest salience item
        if len(self.items) > WORKING_MEMORY_CAPACITY:
            self.items.sort(key=lambda x: x["salience"] * (0.9 ** x["age_ticks"]))
            removed = self.items.pop()
            logger.debug("working_memory: evicted: %s", removed["content"][:40])

    def tick(self):
        """Age all items. Decay salience."""
        for item in self.items:
            item["age_ticks"] += 1
            item["salience"] *= 0.97  # soft decay

        # Remove items older than 60 ticks (~2 min)
        self.items = [i for i in self.items if i["age_ticks"] < 60]

    def get_context(self) -> str:
        """Get current context as text — what the brain is 'thinking about'."""
        if not self.items:
            return ""
        recent = sorted(self.items, key=lambda x: x["salience"] * (0.95 ** x["age_ticks"]), reverse=True)[:3]
        return " | ".join("[{0}] {1}".format(i["source"], i["content"][:80]) for i in recent)

    def get_top(self, n: int = 3) -> list[dict]:
        """Get top n most salient thoughts."""
        return sorted(self.items, key=lambda x: x["salience"] * (0.95 ** x["age_ticks"]), reverse=True)[:n]

    def clear(self):
        self.items.clear()
