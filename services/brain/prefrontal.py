"""前额叶 — 注意力门控、目标对齐、情绪评估、维度推断。

对应人脑：前额叶皮层（PFC）
职责：
  1. 注意力门控（5步决策树）
  2. 重要性/情绪/新颖度/目标相关度评估
  3. 输出通过门控的 MemoryCreate + 调整后的维度
  4. 不通过门控的返回 None（丢弃）
"""
from services.attention_gating import check_gate, GateResult
from services.auto_ingest import (
    estimate_importance,
    estimate_novelty,
    estimate_failure_cost,
    estimate_goal_relevance,
    detect_emotion,
    infer_type,
)
from models.schemas import MemoryCreate


def evaluate_and_gate(memory: MemoryCreate, current_goal: str | None = None) -> tuple[bool, MemoryCreate | None, str]:
    """前额叶评估：门控 + 维度推断。

    Args:
        memory: 输入区产出的候选记忆
        current_goal: 当前目标（可选，用于 goal_relevance 加成）

    Returns:
        (passed, memory_with_dims, reason)
        passed: 是否通过门控
        memory_with_dims: 通过门控的记忆（维度已填充），否则 None
        reason: 门控原因或丢弃原因
    """
    combined = f"{memory.title} {memory.content}"

    # 1. 门控
    gate = check_gate(memory)
    if not gate.passed:
        return False, None, gate.reason

    # 2. 维度推断（前额叶对保留的记忆做精细评估）
    memory.type = infer_type(combined)
    memory.importance = estimate_importance(combined)
    memory.novelty = estimate_novelty(combined)
    memory.failure_cost = estimate_failure_cost(combined)
    memory.goal_relevance = estimate_goal_relevance(combined)
    surprise, emotion_label = detect_emotion(combined)
    memory.surprise_score = surprise

    # 3. 目标加成
    if current_goal:
        memory.goal_relevance = min(memory.goal_relevance + 0.20, 1.0)

    # 4. 显式标记加成
    if memory.explicit_mark:
        memory.importance = max(memory.importance, 0.85)
        gate.reason = "explicit_mark"

    return True, memory, gate.reason
