"""记忆压缩引擎 — v3.0

从情景记忆簇提炼语义摘要，实现"人不是在记住过去，而是一直在重新解释过去"。

触发条件：同一实体簇 >= COMPRESSION_CLUSTER_MIN 条情景记忆
压缩策略：纯规则引擎，不调 LLM，保证精确可追溯
"""
import json
from datetime import datetime, timezone, timedelta
from config import COMPRESSION_CLUSTER_MIN, COMPRESSION_MAX_DAYS
from models.database import get_db


async def compress_episodic_clusters(db, min_cluster_size: int = None) -> dict:
    """压缩情景记忆簇：聚类 → 提炼摘要 → 压缩原文。

    Returns:
        {"clusters_found": int, "compressed": int, "summaries_generated": int}
    """
    if min_cluster_size is None:
        min_cluster_size = COMPRESSION_CLUSTER_MIN

    cutoff = (datetime.now(timezone.utc) - timedelta(days=COMPRESSION_MAX_DAYS)).isoformat()
    now = datetime.now(timezone.utc)

    cursor = await db.execute(
        """SELECT * FROM memories WHERE type = 'episodic' AND archived = 0
           AND created >= ? AND consolidated = 0
           ORDER BY created""",
        (cutoff,),
    )
    episodes = [dict(r) for r in await cursor.fetchall()]

    if len(episodes) < min_cluster_size:
        return {"clusters_found": 0, "compressed": 0, "summaries_generated": 0}

    # Greedy clustering by entity overlap
    clusters = _cluster_by_entities(episodes, min_cluster_size)

    result = {
        "clusters_found": len(clusters),
        "compressed": 0,
        "summaries_generated": 0,
    }

    for cluster in clusters:
        # 生成语义摘要
        title, content = _generate_cluster_summary(cluster)
        common_entities = _extract_common(cluster, "entities", limit=8)
        common_tags = _extract_common(cluster, "tags", limit=5)
        avg_importance = sum(e.get("importance", 0.5) for e in cluster) / len(cluster)

        mem_id = f"semantic-{now.strftime('%Y-%m-%d')}-cp{result['summaries_generated']+1:03d}"

        await db.execute(
            """INSERT INTO memories (id, type, layer, title, content, importance, emotion_weight,
               source, tags, entities, created, access_count, overwrite_weight, region_trace)
               VALUES (?, 'semantic', 'semantic', ?, ?, ?, ?, 'compression', ?, ?, ?, 0, ?, ?)""",
            (
                mem_id, title, content,
                round(avg_importance, 2),
                round(avg_importance * 0.85, 2),
                json.dumps(common_tags, ensure_ascii=False),
                json.dumps(common_entities, ensure_ascii=False),
                now.isoformat(),
                round(avg_importance * 0.7, 4),
                json.dumps(["hippocampus", "storage_zone"], ensure_ascii=False),
            ),
        )

        # 标记原文被压缩，加速衰减
        for ep in cluster:
            compressed_to = json.loads(ep.get("compressed_to", "[]") or "[]")
            if mem_id not in compressed_to:
                compressed_to.append(mem_id)
            await db.execute(
                """UPDATE memories SET compressed_to = ?, decay_rate = decay_rate * 1.5,
                   consolidated = 1, last_consolidated = ? WHERE id = ?""",
                (json.dumps(compressed_to, ensure_ascii=False), now.isoformat(), ep["id"]),
            )

        result["compressed"] += len(cluster)
        result["summaries_generated"] += 1

    await db.commit()
    return result


def _cluster_by_entities(episodes: list[dict], min_size: int) -> list[list[dict]]:
    """贪心聚类：实体重叠率 > 0.4 归入同一簇。"""
    clusters = []
    used = set()

    for i, ep in enumerate(episodes):
        if i in used:
            continue
        cluster = [ep]
        used.add(i)
        ep_entities = set(json.loads(ep.get("entities", "[]") or "[]"))

        if not ep_entities:
            continue

        for j, other in enumerate(episodes):
            if j in used:
                continue
            other_entities = set(json.loads(other.get("entities", "[]") or "[]"))
            if not other_entities:
                continue
            overlap = len(ep_entities & other_entities) / max(len(ep_entities | other_entities), 1)
            if overlap > 0.4:
                cluster.append(other)
                used.add(j)
                ep_entities |= other_entities  # 扩展簇的实体集

        if len(cluster) >= min_size:
            clusters.append(cluster)

    return clusters


def _generate_cluster_summary(cluster: list[dict]) -> tuple[str, str]:
    """从情景记忆簇提炼结构化摘要（纯规则，不调 LLM）。

    摘要包含：
    - 共同实体
    - 时间线（最早→最晚）
    - 关键词交集
    """
    all_entities = set()
    all_tags = set()
    all_keywords = set()
    dates = []

    for ep in cluster:
        all_entities.update(json.loads(ep.get("entities", "[]") or "[]"))
        all_tags.update(json.loads(ep.get("tags", "[]") or "[]"))
        dates.append(ep.get("created", "")[:10])

    # 关键词：标题中重复出现的词
    for ep in cluster:
        words = ep.get("title", "").split()
        for w in words:
            if len(w) >= 2 and w not in ("发现", "修了", "完成", "一个", "的"):
                all_keywords.add(w)

    # 过滤噪音标签
    noise = {"error", "resolved", "decision", "milestone", "breakthrough",
             "source:hermes", "source:cli", "source:unknown"}
    meaningful = all_entities | (all_tags - noise)

    entity_str = ", ".join(sorted(meaningful)[:6]) if meaningful else "general topic"
    date_range = f"{min(dates)} ~ {max(dates)}" if len(dates) > 1 else (dates[0] if dates else "?")
    count = len(cluster)

    title = f"[Compressed] {count} events about {entity_str}"[:80]

    lines = [
        f"Compressed summary of {count} related events ({date_range}).",
        f"Core entities: {entity_str}",
        f"Keywords: {', '.join(sorted(all_keywords)[:8]) or 'none'}",
        "",
        "Source events:",
    ]
    for ep in sorted(cluster, key=lambda x: x.get("created", "")):
        ts = ep.get("created", "")[:10]
        lines.append(f"  [{ts}] {ep.get('title', '')[:80]}")

    return title, "\n".join(lines)


def _extract_common(cluster: list[dict], field: str, limit: int = 8) -> list:
    """提取簇内共同字段（出现频率排序）。"""
    from collections import Counter
    counter = Counter()
    for ep in cluster:
        items = json.loads(ep.get(field, "[]") or "[]")
        counter.update(items)
    return [item for item, _ in counter.most_common(limit)]
