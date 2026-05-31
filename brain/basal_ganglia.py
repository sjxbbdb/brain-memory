"""Basal Ganglia — 基底节。习惯匹配 + 程序化记忆（规则引擎）。

职责:
  1. 维护已知的行为模式/习惯库
  2. 匹配当前上下文到已知模式
  3. 程序化记忆（固化的工作流）

注意: 基底节不做决策，只做模式匹配。决策交给前额叶。
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("brain-v5.basal-ganglia")

# 预置习惯模式
DEFAULT_HABITS = [
    {
        "id": "h-001",
        "trigger": "错误|bug|error|报错|失败",
        "pattern": "遇到技术错误 → 先查时钟/配置/依赖",
        "response": "systematic_debug",
        "confidence": 0.8,
    },
    {
        "id": "h-002",
        "trigger": "修复|fixed|solved|解决|搞定",
        "pattern": "问题解决 → 记录根因 + 预防措施",
        "response": "record_fix",
        "confidence": 0.9,
    },
    {
        "id": "h-003",
        "trigger": "记得|记住|别忘了|重要",
        "pattern": "用户强调记住 → 标记高重要性",
        "response": "high_priority_memory",
        "confidence": 0.85,
    },
    {
        "id": "h-004",
        "trigger": "新项目|创建|新建|开始做",
        "pattern": "新项目启动 → 检查相关记忆 + 建立上下文",
        "response": "context_setup",
        "confidence": 0.7,
    },
]


class BasalGanglia:
    """Habit and procedural memory matching."""

    def __init__(self):
        self.habits: list[dict] = list(DEFAULT_HABITS)
        self.match_log: list[dict] = []

    def match(self, text: str) -> dict | None:
        """Match current input against known habit patterns.

        Returns: matched habit dict or None
        """
        text_lower = text.lower()
        import re

        best_match = None
        best_score = 0.0

        for habit in self.habits:
            if re.search(habit["trigger"], text_lower, re.IGNORECASE):
                score = habit["confidence"]
                if score > best_score:
                    best_score = score
                    best_match = habit

        if best_match and best_score >= 0.6:
            self.match_log.append({
                "habit_id": best_match["id"],
                "trigger_text": text[:100],
                "confidence": best_score,
            })
            if len(self.match_log) > 50:
                self.match_log = self.match_log[-50:]
            logger.debug("basal_ganglia: matched habit %s (%.2f)", best_match["id"], best_score)
            return {"habit": best_match, "confidence": best_score, "matched": True}

        return {"matched": False, "habit": None, "confidence": 0.0}

    def add_habit(self, trigger: str, pattern: str, response: str, confidence: float = 0.6):
        """Learn a new habit pattern."""
        habit = {
            "id": "h-{0:03d}".format(len(self.habits) + 1),
            "trigger": trigger,
            "pattern": pattern,
            "response": response,
            "confidence": confidence,
        }
        self.habits.append(habit)
        logger.info("basal_ganglia: new habit learned: %s", habit["id"])
        return habit

    def reinforce(self, habit_id: str, delta: float = 0.05):
        """Reinforce a habit — increase confidence."""
        for h in self.habits:
            if h["id"] == habit_id:
                h["confidence"] = min(1.0, h["confidence"] + delta)
                return

    def weaken(self, habit_id: str, delta: float = 0.05):
        """Weaken a habit — decrease confidence."""
        for h in self.habits:
            if h["id"] == habit_id:
                h["confidence"] = max(0.1, h["confidence"] - delta)
                return
