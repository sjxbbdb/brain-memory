"""自我感知模块 — 系统主动扫描自身状态，自动形成元认知记忆并推送告警。

类比人脑的"内省"能力：审视自己的状态 → 发现值得记住的模式 → 
形成记忆 + 向外部推送告警。
"""
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("brain-memory.self-awareness")
from services.output_feed import push_alert


async def scan_retrieval_gaps(db) -> list[dict]:
    """扫描最近的零结果检索，找出反复出现的知识缺口。"""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cursor = await db.execute(
        """SELECT query, COUNT(*) as cnt
           FROM retrieval_log
           WHERE timestamp >= ? AND result_count = 0
           GROUP BY query
           HAVING cnt >= 3
           ORDER BY cnt DESC""",
        (one_hour_ago,),
    )
    gaps = await cursor.fetchall()
    return [{"query": r[0], "count": r[1]} for r in gaps]


async def scan_memory_health(db) -> dict:
    """扫描记忆系统健康状况。"""
    cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0")
    total = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 1")
    archived = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT AVG(emotion_weight) FROM memories WHERE archived = 0")
    avg_emotion = (await cursor.fetchone())[0] or 0
    cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0 AND confidence < 0.3")
    low_conf = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0 AND review_needed = 1")
    needs_review = (await cursor.fetchone())[0]
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE created >= ?", (one_hour_ago,))
    recent_new = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM retrieval_log WHERE timestamp >= ?", (one_hour_ago,))
    recent_retrievals = (await cursor.fetchone())[0]
    return {
        "total_active": total, "total_archived": archived,
        "avg_emotion_weight": round(avg_emotion, 3),
        "low_confidence_count": low_conf, "needs_review_count": needs_review,
        "recent_new_memories": recent_new, "recent_retrievals": recent_retrievals,
    }


async def scan_anomalies(db) -> list[dict]:
    """检测异常。"""
    anomalies = []
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memories WHERE archived = 0 AND contradicted_by != '[]'"
    )
    active_conflicts = (await cursor.fetchone())[0]
    if active_conflicts > 0:
        anomalies.append({
            "type": "active_conflicts",
            "detail": f"{active_conflicts} memories have unresolved contradictions",
            "severity": "high" if active_conflicts >= 3 else "medium",
        })
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memories WHERE archived = 0 AND emotion_weight < 0.15"
    )
    endangered = (await cursor.fetchone())[0]
    if endangered > 5:
        anomalies.append({
            "type": "mass_endangerment",
            "detail": f"{endangered} memories at risk of eviction",
            "severity": "high",
        })
    return anomalies


async def auto_ingest_self_events():
    """自我感知主入口：扫描 → 推送告警 → 形成记忆。

    由调度器每 30 分钟调用一次。
    """
    from models.database import get_db
    from services.pipeline import pipeline_ingest

    db = await get_db()
    try:
        events_to_ingest = []

        # 1. 知识缺口 → 推送告警 + 形成记忆
        gaps = await scan_retrieval_gaps(db)
        for gap in gaps:
            push_alert(
                "medium",
                f"知识缺口: {gap['query'][:60]}",
                f"过去1小时 {gap['count']} 次零结果检索",
                {"query": gap["query"]},
            )
            events_to_ingest.append({
                "text": (
                    f"系统发现反复知识缺口：查询 '{gap['query'][:80]}' "
                    f"在过去 1 小时内返回 {gap['count']} 次零结果。"
                    f"此领域知识储备不足，建议补充。"
                ),
                "source": "self-awareness",
                "explicit_mark": True,
                "metadata": {"type": "knowledge_gap", "query": gap["query"]},
            })

        # 2. 异常检测 → 推送告警 + 形成记忆
        anomalies = await scan_anomalies(db)
        for anomaly in anomalies:
            push_alert(
                anomaly["severity"],
                anomaly["type"],
                anomaly["detail"],
                anomaly,
            )
            events_to_ingest.append({
                "text": f"[{anomaly['severity'].upper()}] {anomaly['detail']}",
                "source": "self-awareness",
                "explicit_mark": True,
                "metadata": {"type": "anomaly", "detail": anomaly},
            })

        # 3. 健康快照（6 小时间隔）
        cursor = await db.execute(
            """SELECT COUNT(*) FROM memories
               WHERE source = 'self-awareness'
               AND created >= datetime('now', '-6 hours')"""
        )
        recent_snapshots = (await cursor.fetchone())[0]
        if recent_snapshots == 0:
            health = await scan_memory_health(db)
            events_to_ingest.append({
                "text": (
                    f"[系统健康快照] 活跃记忆: {health['total_active']}, "
                    f"已归档: {health['total_archived']}, "
                    f"平均情绪权重: {health['avg_emotion_weight']}, "
                    f"低置信度: {health['low_confidence_count']}, "
                    f"待审查: {health['needs_review_count']}, "
                    f"最近1h新增: {health['recent_new_memories']}, "
                    f"最近1h检索: {health['recent_retrievals']}"
                ),
                "source": "self-awareness",
                "explicit_mark": True,
                "metadata": {"type": "health_snapshot", "health": health},
            })

    finally:
        await db.close()

    # 摄入所有事件
    ingested = 0
    for event in events_to_ingest:
        try:
            result = await pipeline_ingest(
                text=event["text"],
                source=event["source"],
                metadata=event.get("metadata"),
                bypass_gate=event.get("explicit_mark", False),
            )
            if result.get("accepted"):
                ingested += 1
        except Exception:
            logger.exception("self-awareness: ingest failed")

    if ingested > 0:
        logger.info("self-awareness: %d events auto-ingested", ingested)
    return ingested