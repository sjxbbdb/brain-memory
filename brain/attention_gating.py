"""注意力门控 — 5步决策树。决定一条信息是否值得进入记忆系统。"""
from config import (
    EXPLICIT_MARK_IMPORTANCE,
    GOAL_RELEVANCE_PASS,
    HIGH_EMOTION_DEFAULT,
    EMOTIONAL_KEYWORDS_FAILURE,
    EMOTIONAL_KEYWORDS_BREAKTHROUGH,
    EMOTIONAL_KEYWORDS_CORRECTION,
    EMOTIONAL_KEYWORDS_CONFLICT,
)
from models.schemas import MemoryCreate


class GateResult:
    """门控判定结果。"""

    def __init__(self, passed: bool, reason: str, adjusted_fields: dict | None = None):
        self.passed = passed
        self.reason = reason
        self.adjusted_fields = adjusted_fields or {}


def _has_emotional_signal(text: str) -> tuple[bool, float]:
    """检测高情绪信号，返回 (是否命中, 情绪权重加成)。
    渐变评分：命中关键词越多，加成越高。
    """
    text_lower = text.lower()
    hit_count = 0
    all_keywords = (
        EMOTIONAL_KEYWORDS_FAILURE
        + EMOTIONAL_KEYWORDS_BREAKTHROUGH
        + EMOTIONAL_KEYWORDS_CORRECTION
        + EMOTIONAL_KEYWORDS_CONFLICT
    )
    for kw in all_keywords:
        if kw.lower() in text_lower:
            hit_count += 1
    if hit_count == 0:
        return False, 0.0
    # Gradation: 1 hit → 0.60, 2 hits → 0.72, 3+ hits → 0.85+
    grad = min(0.60 + hit_count * 0.12, HIGH_EMOTION_DEFAULT)
    return True, round(grad, 2)


def check_gate(memory: MemoryCreate) -> GateResult:
    """执行5步注意力门控决策树。

    1. 显式标记 → 直接通过
    2. 高情绪信号 → 通过
    3. 新颖度（new字段值较高）→ 通过
    4. 目标相关度 > 阈值 → 通过
    5. 丢弃
    """
    combined = f"{memory.title} {memory.content}"

    # Step 1: 显式标记
    if memory.explicit_mark:
        return GateResult(True, "explicit_mark", {"importance": EXPLICIT_MARK_IMPORTANCE})

    # Step 2: 高情绪信号
    has_emotion, emotion_val = _has_emotional_signal(combined)
    if has_emotion:
        return GateResult(
            True,
            "emotional_signal",
            {"emotion_weight": emotion_val},
        )

    # Step 3: 新颖度
    if memory.novelty >= 0.7:
        return GateResult(True, "novelty")

    # Step 4: 目标相关度
    if memory.goal_relevance > GOAL_RELEVANCE_PASS:
        return GateResult(True, "goal_relevant")

    # Step 5: 丢弃
    return GateResult(False, "discarded")
