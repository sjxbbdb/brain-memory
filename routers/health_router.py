"""健康检查路由。"""
import json
from fastapi import APIRouter, Query
from models.database import get_db
from models.schemas import HealthOverview, row_to_response
from services.decay import compute_strength, is_endangered

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/overview", response_model=HealthOverview)
async def health_overview():
    db = await get_db()
    try:
        # 总统计
        cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0")
        total = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 1")
        archived = (await cursor.fetchone())[0]

        # 各层数量
        cursor = await db.execute(
            "SELECT layer, COUNT(*) FROM memories WHERE archived = 0 GROUP BY layer"
        )
        layer_counts = {row[0]: row[1] for row in await cursor.fetchall()}

        # 平均强度
        cursor = await db.execute("SELECT * FROM memories WHERE archived = 0")
        rows = await cursor.fetchall()
        strengths = []
        endangered = 0
        low_conf = 0
        conflicts = 0

        for row in rows:
            r = dict(row)
            s = compute_strength(
                r["emotion_weight"], r["decay_rate"],
                r["last_accessed"], r["created"],
            )
            strengths.append(s)
            if is_endangered(s):
                endangered += 1
            if r["confidence"] < 0.5:
                low_conf += 1
            cb = json.loads(r["contradicted_by"] or "[]")
            if cb:
                conflicts += 1

        avg_s = round(sum(strengths) / len(strengths), 4) if strengths else 0

        # 最后巩固时间
        cursor = await db.execute(
            "SELECT timestamp FROM consolidation_log ORDER BY id DESC LIMIT 1"
        )
        last_row = await cursor.fetchone()
        last_con = last_row[0] if last_row else None

        return HealthOverview(
            total_count=total,
            layer_counts=layer_counts,
            avg_strength=avg_s,
            endangered_count=endangered,
            low_confidence_count=low_conf,
            active_conflicts=conflicts,
            last_consolidation=last_con,
            archived_count=archived,
        )
    finally:
        await db.close()


@router.get("/endangered")
async def endangered_memories(limit: int = Query(50, ge=1, le=100)):
    """返回 R(t) < 0.15 的濒危记忆列表。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0 ORDER BY emotion_weight ASC LIMIT ?",
            (limit * 2,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            r = dict(row)
            s = compute_strength(
                r["emotion_weight"], r["decay_rate"],
                r["last_accessed"], r["created"],
            )
            if is_endangered(s):
                resp = row_to_response(r)
                resp.current_strength = s
                result.append(resp)
        return result[:limit]
    finally:
        await db.close()


@router.get("/conflicts")
async def conflict_memories():
    """返回存在活跃冲突的记忆对。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0 AND contradicted_by != '[]'"
        )
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


@router.get("/output-feed")
async def output_feed(limit: int = 20):
    """Read recent entries from output_feed.jsonl."""
    import json as _json
    from pathlib import Path
    feed_path = Path(__file__).parent.parent / "output_feed.jsonl"
    if not feed_path.exists():
        return {"alerts": [], "narratives": [], "total": 0}
    try:
        with open(feed_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        alerts = []
        narratives = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            entry["_raw"] = line
            if entry.get("type") == "alert":
                alerts.append(entry)
            elif entry.get("type") == "narrative":
                narratives.append(entry)
        return {
            "alerts": alerts[-10:],
            "narratives": narratives[-5:],
            "total": len(alerts) + len(narratives),
        }
    except Exception as e:
        return {"alerts": [], "narratives": [], "total": 0, "error": str(e)}
