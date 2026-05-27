"""艾宾浩斯衰减计算 + 检索强化。"""
import math
from datetime import datetime, timezone
from config import (
    DECAY_REDUCTION_FACTOR,
    HALF_LIFE_EXTENSION_FACTOR,
    IMPORTANCE_BOOST,
    STRENGTH_ENDANGERED,
)


def days_since(dt_str: str | None) -> float:
    """计算从 ISO8601 时间字符串到现在的天数。"""
    if dt_str is None:
        return 999.0
    try:
        then = datetime.fromisoformat(dt_str)
        now = datetime.now(timezone.utc)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now - then).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 999.0


def compute_strength(
    emotion_weight: float,
    decay_rate: float,
    last_accessed: str | None,
    created: str,
) -> float:
    """R(t) = emotion_weight * exp(-decay_rate * days_since_last_access)。

    若从未访问，以创建时间为准。
    返回 [0.0, 1.0]。
    """
    ref = last_accessed if last_accessed else created
    d = days_since(ref)
    strength = emotion_weight * math.exp(-decay_rate * d)
    return min(1.0, max(0.0, round(strength, 4)))


def compute_staleness(last_accessed: str | None, created: str) -> float:
    """计算陈旧度：1 - exp(-days/30)，值越高越陈旧。"""
    ref = last_accessed if last_accessed else created
    d = days_since(ref)
    return min(1.0, max(0.0, round(1.0 - math.exp(-d / 30.0), 4)))


def is_endangered(strength: float) -> bool:
    """R(t) < 0.15 为濒危记忆。"""
    return strength < STRENGTH_ENDANGERED


def apply_retrieval_reinforcement(memory_row: dict) -> dict:
    """检索成功后更新衰减字段，返回需要 UPDATE 的字典。

    decay_rate *= 0.85
    half_life_days *= 1.15
    access_count += 1
    importance = min(1.0, importance + 0.01)
    """
    new_decay = memory_row["decay_rate"] * DECAY_REDUCTION_FACTOR
    new_half = memory_row["half_life_days"] * HALF_LIFE_EXTENSION_FACTOR
    new_importance = min(1.0, memory_row["importance"] + IMPORTANCE_BOOST)
    return {
        "decay_rate": round(new_decay, 6),
        "half_life_days": round(new_half, 2),
        "access_count": memory_row["access_count"] + 1,
        "importance": round(new_importance, 4),
        "last_accessed": datetime.now(timezone.utc).isoformat(),
    }
