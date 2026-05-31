"""5维情绪权重计算模块。"""
from config import EMOTION_WEIGHTS


def compute_emotion_weight(
    importance: float,
    failure_cost: float,
    novelty: float,
    goal_relevance: float,
    surprise_score: float,
    weights: dict | None = None,
) -> float:
    """计算综合情绪权重。

    emotion_weight = importance×0.30 + failure_cost×0.25
                   + novelty×0.20 + goal_relevance×0.15
                   + surprise_score×0.10
    结果限制在 [0.0, 1.0]。
    """
    w = weights or EMOTION_WEIGHTS
    score = (
        importance * w["importance"]
        + failure_cost * w["failure_cost"]
        + novelty * w["novelty"]
        + goal_relevance * w["goal_relevance"]
        + surprise_score * w["surprise_score"]
    )
    return min(1.0, max(0.0, score))
