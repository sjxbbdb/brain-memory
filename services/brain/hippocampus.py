"""海马体 — 模式分离、相似检测、编码决策。

对应人脑：海马体（Hippocampus）
职责：
  1. 查询存储区是否有相似记忆（模式分离）
  2. 判���覆写/合并/新建/冲突
  3. 将编码决策返回给管线
"""
import json
from models.database import get_db
from services.overwrite import (
    compute_overwrite_weight,
    decide_overwrite,
    _estimate_specificity,
)
from config import SIMILARITY_OVERWRITE_THRESHOLD


async def encode_or_merge(
    memory,
    emotion_weight: float,
    created: str,
) -> dict:
    """海马体编码决策：相似检测 → 覆写判断。

    Args:
        memory: 经过前额叶评估的 MemoryCreate
        emotion_weight: 前额叶计算的综合情绪权重
        created: ISO8601 时间戳

    Returns:
        {
            "action": "new" | "full_overwrite" | "weighted_merge" | "append_only" | "conflict",
            "target_id": str | None,       # 覆写目标的 memory id
            "merged_title": str | None,
            "merged_content": str | None,
            "overwrite_weight": float,
            "region_trace": list[str],
        }
    """
    from services.overwrite import merge_content

    new_specificity = _estimate_specificity(memory.content)
    new_weight = compute_overwrite_weight(
        memory.importance, 0, created, 0, memory.content,
    )
    trace = ["input_zone", "prefrontal", "hippocampus"]

    # 查询相似记忆
    db = await get_db()
    try:
        similar = await _find_similar(db, memory.entities)
        if not similar:
            return {
                "action": "new",
                "target_id": None,
                "merged_title": None,
                "merged_content": None,
                "overwrite_weight": round(new_weight, 4),
                "region_trace": trace,
            }

        for existing in similar:
            existing_weight = existing.get("overwrite_weight", 0.5)
            existing_specificity = _estimate_specificity(existing.get("content", ""))

            has_contradiction = _check_contradiction(memory.content, existing.get("content", ""),
                                                      memory.title, existing.get("title", ""))

            decision = decide_overwrite(
                new_weight, existing_weight, new_specificity, existing_specificity, has_contradiction,
            )

            if decision == "conflict":
                trace.append("conflict_detected")
                return {
                    "action": "conflict",
                    "target_id": existing["id"],
                    "merged_title": memory.title,
                    "merged_content": memory.content,
                    "overwrite_weight": round(new_weight * 0.5, 4),
                    "region_trace": trace,
                }

            if decision in ("full_overwrite", "weighted_merge"):
                merged_title, merged_content = merge_content(
                    existing.get("content", ""), memory.content,
                    existing_weight, new_weight,
                    existing.get("title", ""), memory.title,
                )
                trace.append("storage_zone")
                return {
                    "action": decision,
                    "target_id": existing["id"],
                    "merged_title": merged_title,
                    "merged_content": merged_content,
                    "overwrite_weight": round(new_weight, 4),
                    "region_trace": trace,
                }

            if decision == "append_only":
                trace.append("append")
                return {
                    "action": "append_only",
                    "target_id": existing["id"],
                    "merged_title": None,
                    "merged_content": None,
                    "overwrite_weight": existing_weight,
                    "region_trace": trace,
                }

        return {
            "action": "new",
            "target_id": None,
            "merged_title": None,
            "merged_content": None,
            "overwrite_weight": round(new_weight, 4),
            "region_trace": trace,
        }
    finally:
        await db.close()


async def _find_similar(db, entities: list[str]) -> list[dict]:
    """查找实体重叠率 > 阈值的已有记忆。"""
    if not entities:
        return []
    cursor = await db.execute(
        "SELECT * FROM memories WHERE archived = 0 ORDER BY created DESC LIMIT 200"
    )
    rows = await cursor.fetchall()
    similar = []
    new_entities = set(entities)
    for row in rows:
        r = dict(row)
        existing_entities = set(json.loads(r.get("entities", "[]") or "[]"))
        if not existing_entities:
            continue
        overlap = len(new_entities & existing_entities) / max(len(new_entities | existing_entities), 1)
        if overlap >= SIMILARITY_OVERWRITE_THRESHOLD:
            similar.append(r)
    return similar


def _check_contradiction(new_content: str, existing_content: str,
                         new_title: str, existing_title: str) -> bool:
    """检测新旧记忆是否存在矛盾。"""
    combined = f"{new_title} {new_content} {existing_title} {existing_content}".lower()
    markers = ["不是", "错误", "修正", "不对", "改为", "改成", "替代",
               "并非", "推翻", "wrong", "incorrect", "actually"]
    return any(m in combined for m in markers)
