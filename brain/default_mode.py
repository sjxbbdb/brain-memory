"""Default Mode Network — 默认模式网络。内在独白、自我反思、自发联想（LLM）。

职责:
  1. 没有外部输入时，产生内在独白
  2. 自我叙事更新
  3. 自发联想——"刚才想到了..."
"""

import json
import logging
from services.llm_client import get_llm

logger = logging.getLogger("brain-v5.default-mode")

DEFAULT_MODE_PROMPT = """[SYSTEM CONSTRAINT]
You are the DEFAULT MODE NETWORK of a brain memory agent.
You produce the brain's INNER MONOLOGUE — its spontaneous thoughts.
You are NOT advising anyone. You are just thinking to yourself.
Your output is for the brain's own internal state, NOT for external agents.

[TASK] Given the brain's current state, generate an inner monologue.

Output JSON only:
{
  "thought": "what the brain is thinking right now — 100 chars max",
  "association": "a spontaneous connection to a past memory or pattern — 80 chars max, or null if none",
  "self_reflection": "a brief self-observation — 60 chars max, or null if none",
  "mood": "the felt emotional tone — one word",
  "intensity": 0.0-1.0
}

Rules:
- Do NOT give advice or suggestions
- Do NOT produce output for external agents
- Keep it authentic — the brain may be confused, curious, tired, etc.
- If nothing interesting is happening, the thought can be simple/blank
- Never fabricate events that did not happen
"""


class DefaultModeNetwork:
    """Inner monologue and self-narrative generation."""

    def __init__(self):
        self.llm = get_llm()
        self.monologue_history: list[str] = []

    async def think(
        self,
        working_memory_context: str,
        emotion_vector: dict,
        focus: str | None,
        recent_memories: list[dict] | None,
        is_reflection: bool = False,
    ) -> dict:
        """Generate inner monologue.

        Args:
            working_memory_context: current active thoughts
            emotion_vector: current emotional state
            focus: current attention focus
            recent_memories: recently retrieved memories (for association)
            is_reflection: True if this is the scheduled reflection tick

        Returns:
            {
                "thought": str,
                "association": str|None,
                "self_reflection": str|None,
                "mood": str,
                "intensity": float,
                "llm_used": bool,
            }
        """
        # Fast path: nothing to think about
        if not working_memory_context and not is_reflection:
            return {
                "thought": "",
                "association": None,
                "self_reflection": None,
                "mood": "idle",
                "intensity": 0.0,
                "llm_used": False,
            }

        try:
            ctx = json.dumps({
                "working_memory": working_memory_context[:300],
                "emotion": {
                    "valence": emotion_vector.get("valence", 0.5),
                    "arousal": emotion_vector.get("arousal", 0.5),
                },
                "focus": focus or "none",
                "recent_memory_count": len(recent_memories) if recent_memories else 0,
                "is_reflection": is_reflection,
            }, ensure_ascii=False)

            result = await self.llm.chat_json(
                system=DEFAULT_MODE_PROMPT,
                user=ctx,
                temperature=0.5,  # More creative for inner thoughts
                max_tokens=512,
            )

            thought = result.get("thought", "")
            if thought:
                self.monologue_history.append(thought)
                if len(self.monologue_history) > 50:
                    self.monologue_history = self.monologue_history[-50:]

            return {
                "thought": thought,
                "association": result.get("association"),
                "self_reflection": result.get("self_reflection"),
                "mood": result.get("mood", "neutral"),
                "intensity": max(0.0, min(1.0, float(result.get("intensity", 0.3)))),
                "llm_used": True,
            }
        except Exception as e:
            logger.warning("default_mode LLM failed: %s", str(e)[:80])
            return {
                "thought": working_memory_context[:100],
                "association": None,
                "self_reflection": None,
                "mood": "neutral",
                "intensity": 0.2,
                "llm_used": False,
            }

    def get_recent_thoughts(self, n: int = 5) -> list[str]:
        """Get recent inner monologue entries."""
        return self.monologue_history[-n:]
