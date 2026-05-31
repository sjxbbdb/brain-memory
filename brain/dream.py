"""Dream Engine — 梦境生成器。睡眠期间产生自由联想叙事。

职责:
  1. 从记忆中随机采样片段
  2. 用 LLM 编织成梦境叙事
  3. 梦境可能产生意外关联 → 新的语义记忆
"""

import logging
import random
from datetime import datetime, timezone
from services.llm_client import get_llm

logger = logging.getLogger("brain-v5.dream")

DREAM_PROMPT = """[SYSTEM CONSTRAINT]
You are the DREAMING BRAIN. You are synthesizing a dream from memory fragments.
This is internal brain activity — NOT output for external agents.
Produce a surreal, associative narrative weaving the fragments together.

Output JSON only:
{
  "dream_title": "dream title, 15 chars",
  "narrative": "dream narrative weaving the fragments, 200 chars max",
  "key_image": "the most vivid image in the dream, 30 chars max",
  "emotional_tone": "one word describing the dream mood",
  "significance": 0.0-1.0  // how meaningful this dream feels
}

Rules:
- Weave fragments creatively but don't fabricate new factual content
- Dreams can be surreal, metaphorical, emotionally charged
- If fragments don't connect, produce a short abstract dream
"""


class DreamEngine:
    """Dream generation during sleep."""

    def __init__(self):
        self.llm = get_llm()
        self.dream_log: list[dict] = []

    async def dream(self, memory_store, working_memory_snapshot: list[str]) -> dict:
        """Generate a dream from random memory fragments.

        Args:
            memory_store: MemoryStore for fetching random memories
            working_memory_snapshot: recent thoughts before sleep

        Returns: dream dict or None
        """
        if not memory_store:
            return None

        try:
            # Get recent memories as dream material
            import sqlite3, json as _json
            conn = sqlite3.connect(memory_store.db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT title, summary, type, importance, emotion_label FROM memories "
                    "WHERE archived=0 ORDER BY RANDOM() LIMIT 8"
                ).fetchall()
                fragments = [dict(r) for r in rows]
            finally:
                conn.close()

            if not fragments and not working_memory_snapshot:
                return None

            # Build dream context
            context_parts = []
            if fragments:
                context_parts.append("Memory fragments:")
                for f in fragments:
                    context_parts.append("  [{0}] {1}: {2}".format(
                        f.get("type", ""), f.get("title", ""),
                        (f.get("summary", "") or "")[:80],
                    ))
            if working_memory_snapshot:
                context_parts.append("Recent thoughts: " + " | ".join(working_memory_snapshot[:3]))

            context = "\n".join(context_parts)[:3000]

            result = await self.llm.chat_json(
                system=DREAM_PROMPT,
                user=context,
                temperature=0.7,  # Creative temperature for dreams
                max_tokens=512,
            )

            dream = {
                "dream_title": result.get("dream_title", "untitled"),
                "narrative": result.get("narrative", ""),
                "key_image": result.get("key_image", ""),
                "emotional_tone": result.get("emotional_tone", "neutral"),
                "significance": max(0.0, min(1.0, float(result.get("significance", 0.3)))),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "fragment_count": len(fragments),
            }

            self.dream_log.append(dream)
            if len(self.dream_log) > 20:
                self.dream_log = self.dream_log[-20:]

            logger.info("dream: generated '%s' (%d fragments)", dream["dream_title"], dream["fragment_count"])
            return dream
        except Exception as e:
            logger.warning("dream generation failed: %s", str(e)[:80])
            return None

    def get_recent_dreams(self, n: int = 3) -> list[dict]:
        return self.dream_log[-n:]
