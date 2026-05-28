"""存储区 — 记忆持久化、覆写执行、压缩触发。

对应人脑：新皮层（Neocortex）分布式长时记忆存储
职责：
  1. 执行海马体的编码决策（新建/覆写/合并）
  2. 写入 memory_versions 冷归档
  3. 检查是否触发压缩阈值
  4. 返回持久化结果
"""
import json
from datetime import datetime, timezone
from models.database import get_db
from config import COMPRESSION_CLUSTER_MIN


async def commit_to_storage(encode_result: dict, memory, emotion_weight: float, data: dict) -> dict:
    """存储区提交：执行海马体的编码决策。

    Args:
        encode_result: 海马体 encode_or_merge() 返回的决策
        memory: MemoryCreate 对象
        emotion_weight: 情绪权重
        data: 原始数据字典（包含 tags, entities, relations 的 JSON 字符串）

    Returns:
        {"memory_id": str, "action": str, "version": int, "is_compression_triggered": bool}
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    action = encode_result["action"]
    trace = encode_result.get("region_trace", [])
    trace.append("storage_zone")

    try:
        if action == "new":
            mem_id = await _generate_id(db, memory.type)
            await db.execute(
                """INSERT INTO memories (id, type, layer, title, content,
                   importance, novelty, failure_cost, goal_relevance, emotion_weight,
                   surprise_score, confidence, source, tags, entities, relations,
                   created, overwrite_weight, region_trace)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id, memory.type, memory.type, memory.title, memory.content,
                    memory.importance, memory.novelty, memory.failure_cost,
                    memory.goal_relevance, emotion_weight, memory.surprise_score,
                    memory.confidence, memory.source,
                    data.get("tags", "[]"), data.get("entities", "[]"),
                    data.get("relations", "[]"), now,
                    encode_result["overwrite_weight"],
                    json.dumps(trace, ensure_ascii=False),
                ),
            )
            await db.commit()
            return {
                "memory_id": mem_id, "action": "new", "version": 1,
                "is_compression_triggered": await _check_compression_trigger(db, memory.type),
            }

        target_id = encode_result["target_id"]
        if not target_id:
            return {"memory_id": None, "action": "error", "version": 0, "is_compression_triggered": False}

        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (target_id,))
        existing_row = await cursor.fetchone()
        if not existing_row:
            return {"memory_id": None, "action": "error", "version": 0, "is_compression_triggered": False}

        existing = dict(existing_row)

        if action == "full_overwrite":
            await _save_version(db, existing, "full_overwrite", now)
            new_version = existing.get("version", 1) + 1
            await db.execute(
                """UPDATE memories SET title=?, content=?, overwrite_weight=?,
                   version=?, parent_id=?, access_count=access_count+1,
                   last_accessed=?, region_trace=?
                   WHERE id=?""",
                (
                    encode_result["merged_title"], encode_result["merged_content"],
                    encode_result["overwrite_weight"], new_version, "overwrite",
                    now, json.dumps(trace, ensure_ascii=False), target_id,
                ),
            )
            await db.commit()
            return {
                "memory_id": target_id, "action": "full_overwrite", "version": new_version,
                "is_compression_triggered": False,
            }

        elif action == "weighted_merge":
            await _save_version(db, existing, "weighted_merge", now)
            merged_weight = (existing.get("overwrite_weight", 0.5) + encode_result["overwrite_weight"]) / 2
            new_version = existing.get("version", 1) + 1
            merged_entities = list(set(
                json.loads(existing.get("entities", "[]") or "[]") +
                json.loads(data.get("entities", "[]") or "[]")
            ))
            merged_tags = list(set(
                json.loads(existing.get("tags", "[]") or "[]") +
                json.loads(data.get("tags", "[]") or "[]")
            ))
            await db.execute(
                """UPDATE memories SET title=?, content=?, overwrite_weight=?,
                   version=?, parent_id=?, tags=?, entities=?,
                   access_count=access_count+1, last_accessed=?, region_trace=?
                   WHERE id=?""",
                (
                    encode_result["merged_title"], encode_result["merged_content"],
                    round(merged_weight, 4), new_version, "weighted_merge",
                    json.dumps(merged_tags, ensure_ascii=False),
                    json.dumps(merged_entities, ensure_ascii=False),
                    now, json.dumps(trace, ensure_ascii=False), target_id,
                ),
            )
            await db.commit()
            return {
                "memory_id": target_id, "action": "weighted_merge", "version": new_version,
                "is_compression_triggered": False,
            }

        elif action == "append_only":
            merged_entities = list(set(
                json.loads(existing.get("entities", "[]") or "[]") +
                json.loads(data.get("entities", "[]") or "[]")
            ))
            await db.execute(
                """UPDATE memories SET access_count=access_count+1, last_accessed=?,
                   entities=?, region_trace=?
                   WHERE id=?""",
                (
                    now, json.dumps(merged_entities, ensure_ascii=False),
                    json.dumps(trace, ensure_ascii=False), target_id,
                ),
            )
            await db.commit()
            return {
                "memory_id": target_id, "action": "append_only",
                "version": existing.get("version", 1),
                "is_compression_triggered": False,
            }

        elif action == "conflict":
            await db.execute(
                "UPDATE memories SET confidence = ?, review_needed = 1 WHERE id = ?",
                (round(existing.get("confidence", 0.8) * 0.5, 4), target_id),
            )
            mem_id = await _generate_id(db, memory.type)
            await db.execute(
                """INSERT INTO memories (id, type, layer, title, content,
                   importance, emotion_weight, confidence, source,
                   tags, entities, relations, created, overwrite_weight,
                   review_needed, region_trace)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    mem_id, memory.type, memory.type, memory.title, memory.content,
                    memory.importance, emotion_weight,
                    round(memory.confidence * 0.5, 4), memory.source,
                    data.get("tags", "[]"), data.get("entities", "[]"),
                    data.get("relations", "[]"), now,
                    round(encode_result["overwrite_weight"], 4),
                    json.dumps(trace, ensure_ascii=False),
                ),
            )
            await db.commit()
            return {
                "memory_id": mem_id, "action": "conflict", "version": 1,
                "is_compression_triggered": False,
            }

    finally:
        await db.close()


async def _save_version(db, memory_row: dict, reason: str, now: str):
    """将当前记忆快照存入 memory_versions 冷归档。"""
    snapshot = json.dumps(dict(memory_row), ensure_ascii=False, separators=(",", ":"))
    await db.execute(
        """INSERT INTO memory_versions (memory_id, version, title, content,
           overwrite_weight, overwrite_reason, created, snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_row["id"],
            memory_row.get("version", 1),
            memory_row["title"],
            memory_row["content"],
            memory_row.get("overwrite_weight", 0.5),
            reason,
            now,
            snapshot,
        ),
    )


async def _generate_id(db, layer: str) -> str:
    """生成记忆 ID。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"{layer}-{today}-"
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memories WHERE id LIKE ?", (f"{prefix}%",)
    )
    row = await cursor.fetchone()
    count = row[0] + 1
    return f"{prefix}{count:03d}"


async def _check_compression_trigger(db, mem_type: str) -> bool:
    """检查是否累计足够情景记忆触发压缩。"""
    if mem_type != "episodic":
        return False
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memories WHERE type='episodic' AND archived=0 AND consolidated=0"
    )
    row = await cursor.fetchone()
    return row[0] >= COMPRESSION_CLUSTER_MIN
