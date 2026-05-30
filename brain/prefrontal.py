"""Prefrontal Cortex — 前额叶。执行功能：决策、目标管理、注意力分配（LLM）。

职责:
  1. 根据杏仁核情绪 + 海马体检索结果 + 工作记忆 → 决定"现在该关注什么"
  2. 管理目标栈
  3. 输出注意力焦点 + 决策
"""

import json
import logging
from services.llm_client import get_llm

logger = logging.getLogger("brain-v4.prefrontal")

PREFRONTAL_PROMPT = """[SYSTEM CONSTRAINT]
You are the PREFRONTAL CORTEX of a brain memory agent. You are part of the brain, NOT an external assistant.
Your ONLY job: decide what the brain should focus on right now.

Output JSON only:
{
  "focus": "what to focus on — 15 chars max",
  "decision": "the decision or action to take",
  "goal_update": "updated goal, if changed, or null",
  "priority": "high|medium|low",
  "reasoning": "brief thought process — 50 chars max"
}

Rules:
- If input contains a question/request → focus on answering it
- If input reports a problem → focus on understanding root cause
- If input marks something important → set high priority
- If no external input → generate a self-directed focus based on working memory
- Never fabricate content not in the input
- Never give advice — only decide WHAT to focus on
"""


class Prefrontal:
    """Executive function — decision-making and attention control."""

    def __init__(self):
        self.llm = get_llm()
        self.focus_history: list[str] = []

    async def deliberate(
        self,
        thalamus_output: dict,
        amygdala_output: dict,
        hippocampus_output: dict | None,
        working_memory_context: str,
        current_state: dict,
    ) -> dict:
        """Executive deliberation — decide what to focus on.

        Returns:
            {
                "focus": str,
                "decision": str,
                "goal_update": str|None,
                "priority": str,
                "reasoning": str,
                "llm_used": bool,
            }
        """
        has_input = thalamus_output.get("has_input", False)
        priority = thalamus_output.get("priority", "normal")
        salience = amygdala_output.get("salience", 0.0)
        urgent = amygdala_output.get("urgent", False)

        # Fast path: urgent + high salience → no LLM needed
        if urgent and salience > 0.7:
            return {
                "focus": thalamus_output.get("text", "")[:80],
                "decision": "immediate_attention",
                "goal_update": None,
                "priority": "high",
                "reasoning": "urgent high-salience input, immediate focus required",
                "llm_used": False,
            }

        # Fast path: no input + no significant working memory → idle
        if not has_input and not working_memory_context:
            return {
                "focus": None,
                "decision": "idle",
                "goal_update": None,
                "priority": "low",
                "reasoning": "no input and empty working memory",
                "llm_used": False,
            }

        # LLM path: complex decision needed
        try:
            context = json.dumps({
                "has_input": has_input,
                "input_text": thalamus_output.get("text", "")[:500],
                "priority": priority,
                "emotion": amygdala_output.get("emotion_label", "neutral"),
                "salience": salience,
                "memory_hits": len(hippocampus_output.get("results", [])) if hippocampus_output else 0,
                "working_memory": working_memory_context[:300],
                "current_goal": current_state.get("current_goal", "none"),
            }, ensure_ascii=False)

            result = await self.llm.chat_json(
                system=PREFRONTAL_PROMPT,
                user=context,
                temperature=0.3,
                max_tokens=512,
            )

            focus = result.get("focus", thalamus_output.get("text", "")[:80])
            self.focus_history.append(focus)
            if len(self.focus_history) > 20:
                self.focus_history = self.focus_history[-20:]

            return {
                "focus": focus,
                "decision": result.get("decision", "process"),
                "goal_update": result.get("goal_update"),
                "priority": result.get("priority", priority),
                "reasoning": result.get("reasoning", ""),
                "llm_used": True,
            }
        except Exception as e:
            logger.warning("prefrontal LLM failed: %s", str(e)[:80])
            return {
                "focus": thalamus_output.get("text", "")[:80] if has_input else None,
                "decision": "process" if has_input else "idle",
                "goal_update": None,
                "priority": priority,
                "reasoning": "llm failed, default decision",
                "llm_used": False,
            }
