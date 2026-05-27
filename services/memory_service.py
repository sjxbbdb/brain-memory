"""记忆 CRUD 服务 — 写入、查询、更新、删除。"""
import json
from datetime import datetime, timezone
from models.database import get_db
from models.schemas import MemoryCreate, MemoryUpdate, MemoryResponse, row_to_response
from services.emotion_weight import compute_emotion_weight
from services.decay import compute_strength


def _json_compact(obj) -> str:
    """紧凑 JSON 序列化。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def generate_id(db, layer: str) -> str:
    """生成 ID，格式: episodic-2026-05-27-001"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"{layer}-{today}-"
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memories WHERE id LIKE ?", (f"{prefix}%",)
    )
    row = await cursor.fetchone()
    count = row[0] + 1
    return f"{prefix}{count:03d}"


async def create_memory(data: MemoryCreate) -> MemoryResponse:
    """创建记忆，包含注意力门控筛查（在 router 层调用）。"""
    db = await get_db()
    try:
        emotion = compute_emotion_weight(
            data.importance, data.failure_cost,
            data.novelty, data.goal_relevance, data.surprise_score,
        )
        mem_id = await generate_id(db, data.type)
        now = datetime.now(timezone.utc).isoformat()

        await db.execute(
            """
            INSERT INTO memories (id, type, layer, title, content,
                importance, novelty, failure_cost, goal_relevance,
                emotion_weight, surprise_score, confidence, source,
                tags, entities, relations, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mem_id, data.type, data.type, data.title, data.content,
                data.importance, data.novelty, data.failure_cost,
                data.goal_relevance, emotion, data.surprise_score,
                data.confidence, data.source,
                _json_compact(data.tags),
                _json_compact(data.entities),
                _json_compact(data.relations),
                now,
            ),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,))
        row = await cursor.fetchone()
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate,
            resp.last_accessed, resp.created,
        )
        return resp
    finally:
        await db.close()


async def get_memory(memory_id: str) -> MemoryResponse | None:
    """获取单条记忆，含实时强度计算。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate,
            resp.last_accessed, resp.created,
        )
        return resp
    finally:
        await db.close()


async def list_memories(
    layer: str | None = None,
    type_: str | None = None,
    sort_by: str = "created",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
) -> list[MemoryResponse]:
    """分页列表查询，支持按层、类型筛选。"""
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

        valid_sorts = {"created", "emotion_weight", "confidence", "access_count", "title"}
        if sort_by not in valid_sorts:
            sort_by = "created"
        direction = "DESC" if order.upper() == "DESC" else "ASC"

        sql = f"SELECT * FROM memories {where_clause} ORDER BY {sort_by} {direction} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            resp = row_to_response(dict(row))
            resp.current_strength = compute_strength(
                resp.emotion_weight, resp.decay_rate,
                resp.last_accessed, resp.created,
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
    """获取符合条件的记忆总数。"""
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
    """部分更新记忆字段。若维度字段变更则重算 emotion_weight。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        if row is None:
            return None

        existing = dict(row)
        updates = {}

        # 可更新的文本字段
        for field in ("title", "content", "source"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = val

        # JSON 字段
        for field in ("tags", "entities", "relations"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = _json_compact(val)

        # 数值字段
        dim_changed = False
        for field in ("importance", "novelty", "failure_cost", "goal_relevance", "surprise_score", "confidence"):
            val = getattr(data, field, None)
            if val is not None:
                updates[field] = val
                if field != "confidence":
                    dim_changed = True

        # type 变更时同步 layer
        if data.type is not None:
            updates["type"] = data.type
            updates["layer"] = data.type

        if not updates:
            resp = row_to_response(existing)
            resp.current_strength = compute_strength(
                resp.emotion_weight, resp.decay_rate,
                resp.last_accessed, resp.created,
            )
            return resp

        # 维度变更→重算情绪权重
        if dim_changed:
            merged = {**existing, **updates}
            updates["emotion_weight"] = compute_emotion_weight(
                merged.get("importance", 0.5),
                merged.get("failure_cost", 0.5),
                merged.get("novelty", 0.5),
                merged.get("goal_relevance", 0.5),
                merged.get("surprise_score", 0.3),
            )

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [memory_id]
        await db.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
        await db.commit()

        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        resp = row_to_response(dict(row))
        resp.current_strength = compute_strength(
            resp.emotion_weight, resp.decay_rate,
            resp.last_accessed, resp.created,
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
