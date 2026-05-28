"""记忆 CRUD 服务 — v3.0 脑区管线集成。

create_memory 走完整脑区管线：海马体(编码决策) → 存储区(持久化)
其他 CRUD (list/get/update/archive/delete/search) 直接操作 DB。
"""
import json
from datetime import datetime, timezone
from models.database import get_db
from models.schemas import MemoryCreate, MemoryUpdate, MemoryResponse, row_to_response
from services.emotion_weight import compute_emotion_weight
from services.decay import compute_strength
from services.brain.hippocampus import encode_or_merge
from services.brain.storage_zone import commit_to_storage


def _json_compact(obj) -> str:
    """紧凑 JSON 序列化。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def create_memory(data: MemoryCreate) -> MemoryResponse:
    """创建记忆 — 走完整脑区管线：海马体(模式分离+编码决策) → 存储区(持久化)。

    入库前海马体自动检测实体重叠的已有记忆，按覆写决策矩阵处理：
    - new: 全新创建
    - full_overwrite: 全量覆写，旧版进 memory_versions
    - weighted_merge: 加权合并
    - append_only: 追加补充
    - conflict: 标记矛盾
    """
    now = datetime.now(timezone.utc).isoformat()
    emotion = compute_emotion_weight(
        data.importance, data.failure_cost,
        data.novelty, data.goal_relevance, data.surprise_score,
    )

    # Stage 1: 海马体 — 模式分离 + 编码决策
    encode_result = await encode_or_merge(data, emotion, now)

    # Stage 2: 存储区 — 持久化
    store_data = {
        "tags": _json_compact(data.tags),
        "entities": _json_compact(data.entities),
        "relations": _json_compact(data.relations),
    }
    store_result = await commit_to_storage(encode_result, data, emotion, store_data)

    # Stage 3: 读回完整记忆
    mem_id = store_result["memory_id"]
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,))
        row = await cursor.fetchone()
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate, resp.last_accessed, resp.created,
        )
        return resp
    finally:
        await db.close()


async def get_memory(memory_id: str) -> MemoryResponse | None:
    """获取单条记忆并计算实时衰减强度。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate, resp.last_accessed, resp.created,
        )
        return resp
    finally:
        await db.close()


async def list_memories(
    layer: str | None = None,
    type_: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[MemoryResponse]:
    """列出记忆，支持层级和类型筛选。"""
    db = await get_db()
    try:
        where = []
        params = []
        if layer:
            where.append("layer = ?")
            params.append(layer)
        if type_:
            where.append("type = ?")
            params.append(type_)
        if not include_archived:
            where.append("archived = 0")
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        cursor = await db.execute(
            f"SELECT * FROM memories {where_clause} ORDER BY created DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            resp = row_to_response(dict(row))
            resp.current_strength = compute_strength(
                resp.emotion_weight, resp.decay_rate, resp.last_accessed, resp.created,
            )
            results.append(resp)
        return results
    finally:
        await db.close()


async def count_memories(
    layer: str | None = None,
    type_: str | None = None,
    include_archived: bool = False,
) -> int:
    """统计符合条件的记忆总数。"""
    db = await get_db()
    try:
        where = []
        params = []
        if layer:
            where.append("layer = ?")
            params.append(layer)
        if type_:
            where.append("type = ?")
            params.append(type_)
        if not include_archived:
            where.append("archived = 0")
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        cursor = await db.execute(f"SELECT COUNT(*) FROM memories {where_clause}", params)
        row = await cursor.fetchone()
        return row[0]
    finally:
        await db.close()


async def update_memory(memory_id: str, data: MemoryUpdate) -> MemoryResponse | None:
    """部分更新记忆字段。若维度字段变更则重算 emotion_weight + overwrite_weight。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            return None

        existing = dict(row)
        updates = {}

        for field in ("title", "content", "source"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = val

        for field in ("tags", "entities", "relations"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = _json_compact(val)

        dim_changed = False
        for field in ("importance", "novelty", "failure_cost", "goal_relevance", "surprise_score", "confidence"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = val
                if field != "confidence":
                    dim_changed = True

        if data.type is not None:
            updates["type"] = data.type
            updates["layer"] = data.type

        if not updates:
            resp = row_to_response(existing)
            resp.current_strength = compute_strength(
                resp.emotion_weight, resp.decay_rate, resp.last_accessed, resp.created,
            )
            return resp

        if dim_changed:
            merged = {**existing, **updates}
            updates["emotion_weight"] = compute_emotion_weight(
                merged.get("importance", 0.5),
                merged.get("failure_cost", 0.5),
                merged.get("novelty", 0.5),
                merged.get("goal_relevance", 0.5),
                merged.get("surprise_score", 0.3),
            )

        # v3.0: 更新时同步重算覆写权值
        if "content" in updates or dim_changed:
            from services.overwrite import compute_overwrite_weight
            merged = {**existing, **updates}
            updates["overwrite_weight"] = compute_overwrite_weight(
                merged.get("importance", 0.5),
                merged.get("corroboration_count", 0),
                merged.get("created", ""),
                merged.get("access_count", 0),
                merged.get("content", ""),
            )

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [memory_id]
        await db.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
        await db.commit()

        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate, resp.last_accessed, resp.created,
        )
        return resp
    finally:
        await db.close()


async def archive_memory(memory_id: str) -> bool:
    """软删除：标记 archived=1。"""
    db = await get_db()
    try:
        cursor = await db.execute("UPDATE memories SET archived = 1 WHERE id = ?", (memory_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def delete_memory(memory_id: str) -> bool:
    """硬删除（仅限已归档的记忆）。"""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM memories WHERE id = ? AND archived = 1", (memory_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def search_by_keywords(db, keywords: list[str], limit: int = 50) -> list[MemoryResponse]:
    """关键词匹配预筛选（用于检索服务）。"""
    if not keywords:
        return []
    clauses = []
    params = []
    for kw in keywords:
        like = f"%{kw}%"
        clauses.append("(title LIKE ? OR content LIKE ? OR tags LIKE ?)")
        params.extend([like, like, like])
    sql = f"SELECT * FROM memories WHERE archived = 0 AND ({' OR '.join(clauses)}) LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [row_to_response(dict(r)) for r in rows]