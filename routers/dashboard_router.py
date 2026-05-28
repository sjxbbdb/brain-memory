"""仪表盘数据路由。"""
import math
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query
from models.database import get_db
from models.schemas import row_to_response
from services.decay import compute_strength

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def dashboard_stats():
    """仪表盘头部精简统计。"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0")
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT layer, COUNT(*) FROM memories WHERE archived = 0 GROUP BY layer"
        )
        layers = {row[0]: row[1] for row in await cursor.fetchall()}

        cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 1")
        archived = (await cursor.fetchone())[0]

        # 平均强度
        cursor = await db.execute("SELECT * FROM memories WHERE archived = 0")
        rows = await cursor.fetchall()
        strengths = []
        endangered = 0
        for row in rows:
            r = dict(row)
            s = compute_strength(
                r["emotion_weight"], r["decay_rate"],
                r["last_accessed"], r["created"],
            )
            strengths.append(s)
            if s < 0.15:
                endangered += 1
        avg_s = round(sum(strengths) / len(strengths), 3) if strengths else 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM memories WHERE archived = 0 AND contradicted_by != '[]'"
        )
        conflicts = (await cursor.fetchone())[0]

        # v2.1: aggregation stats
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memories WHERE source = 'aggregation' AND archived = 0"
        )
        aggregated = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            "SELECT COUNT(*) FROM consolidation_log WHERE phase = 'phase28'"
        )
        aggregation_runs = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            "SELECT COUNT(*) FROM retrieval_log WHERE feedback != 0"
        )
        feedback_count = (await cursor.fetchone())[0]
        
        return {
            "total": total,
            "archived": archived,
            "layers": layers,
            "avg_strength": avg_s,
            "endangered": endangered,
            "conflicts": conflicts,
            "aggregated": aggregated,
            "aggregation_runs": aggregation_runs,
            "feedback_count": feedback_count,
            "version": "2.1.0",
        }
    finally:
        await db.close()


@router.get("/decay-curve")
async def decay_curve(days: int = Query(90, ge=7, le=365)):
    """衰减曲线数据：按层级和天数返回投影强度。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0"
        )
        rows = await cursor.fetchall()
        memories = [dict(r) for r in rows]

        # 按层分组
        by_layer: dict[str, list] = {}
        for m in memories:
            layer = m["layer"]
            if layer not in by_layer:
                by_layer[layer] = []
            by_layer[layer].append(m)

        now = datetime.now(timezone.utc)
        data_points = []

        for day_offset in range(0, days + 1, max(1, days // 30)):
            date = now - timedelta(days=days - day_offset)
            point = {
                "day": day_offset,
                "date": date.strftime("%m-%d"),
                "layers": {},
                "overall": 0,
            }

            all_vals = []
            for layer, mems in by_layer.items():
                vals = []
                for m in mems:
                    # 模拟未来衰减
                    days_since = (date - datetime.fromisoformat(m["created"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400
                    days_since = max(0, days_since)
                    strength = m["emotion_weight"] * math.exp(-m["decay_rate"] * days_since)
                    vals.append(strength)
                avg = sum(vals) / len(vals) if vals else 0
                point["layers"][layer] = round(avg, 4)
                all_vals.extend(vals)

            point["overall"] = round(sum(all_vals) / len(all_vals), 4) if all_vals else 0
            point["threshold"] = 0.15
            data_points.append(point)

        return {
            "days": days,
            "layers": list(by_layer.keys()),
            "points": data_points,
        }
    finally:
        await db.close()


@router.get("/timeline")
async def timeline(days: int = Query(7, ge=1, le=90), limit: int = Query(30, ge=1, le=100)):
    """记忆创建时间线数据。"""
    db = await get_db()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0 AND created >= ? ORDER BY created DESC LIMIT ?",
            (cutoff, limit),
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
