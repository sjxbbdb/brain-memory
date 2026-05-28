"""叙事生成器 — 将情景记忆自动编织为自我叙事。

类比人脑默认模式网络（DMN）：在"空闲"时自动整合近期经历，
形成连贯的自我叙事——"我是一个遇到并解决了这些问题的开发者"。

触发：每 6 小时，扫描最近 24 小时的情景记忆
产出：narrative 层记忆，作为系统的"自传"
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from collections import Counter

logger = logging.getLogger("brain-memory.narrative")


async def generate_narratives(db) -> dict:
    """扫描近期情景记忆，生成叙事摘要。

    Returns:
        {"scanned": int, "clusters": int, "narratives_generated": int}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    now = datetime.now(timezone.utc)

    cursor = await db.execute(
        """SELECT * FROM memories
           WHERE type = 'episodic' AND archived = 0
           AND created >= ?
           ORDER BY created""",
        (cutoff,),
    )
    episodes = [dict(r) for r in await cursor.fetchall()]

    if len(episodes) < 3:
        return {"scanned": len(episodes), "clusters": 0, "narratives_generated": 0}

    # 按实体聚类
    clusters = _cluster_episodes(episodes)

    generated = 0
    for cluster in clusters:
        if len(cluster) < 2:
            continue

        narrative = _weave_narrative(cluster)
        if not narrative:
            continue

        mem_id = f"narrative-{now.strftime('%Y-%m-%d')}-n{generated+1:03d}"

        common_entities = _extract_common(cluster, "entities", limit=8)
        common_tags = _extract_common(cluster, "tags", limit=5)
        avg_importance = sum(e.get("importance", 0.5) for e in cluster) / len(cluster)

        await db.execute(
            """INSERT INTO memories (id, type, layer, title, content,
               importance, emotion_weight, source, tags, entities,
               created, access_count, overwrite_weight, region_trace)
               VALUES (?, 'narrative', 'narrative', ?, ?, ?, ?, 'narrative-generator',
               ?, ?, ?, 0, ?, ?)""",
            (
                mem_id,
                narrative["title"],
                narrative["content"],
                round(avg_importance, 2),
                round(avg_importance * 0.9, 2),
                json.dumps(common_tags, ensure_ascii=False),
                json.dumps(common_entities, ensure_ascii=False),
                now.isoformat(),
                round(avg_importance * 0.8, 4),
                json.dumps(["hippocampus", "prefrontal", "storage_zone"], ensure_ascii=False),
            ),
        )

        # 标记源情景记忆已被用于叙事
        for ep in cluster:
            consolidated_to = json.loads(ep.get("consolidated_to", "[]") or "[]")
            if mem_id not in consolidated_to:
                consolidated_to.append(mem_id)
            await db.execute(
                "UPDATE memories SET consolidated_to = ? WHERE id = ?",
                (json.dumps(consolidated_to, ensure_ascii=False), ep["id"]),
            )

        generated += 1
        logger.info("narrative: generated — %s", narrative["title"][:60])

    await db.commit()
    return {
        "scanned": len(episodes),
        "clusters": len(clusters),
        "narratives_generated": generated,
    }


def _cluster_episodes(episodes: list[dict]) -> list[list[dict]]:
    """按实体重叠将情景记忆聚簇。"""
    clusters = []
    used = set()

    for i, ep in enumerate(episodes):
        if i in used:
            continue
        cluster = [ep]
        used.add(i)
        ep_entities = set(json.loads(ep.get("entities", "[]") or "[]"))

        for j, other in enumerate(episodes):
            if j in used:
                continue
            other_entities = set(json.loads(other.get("entities", "[]") or "[]"))
            if not ep_entities or not other_entities:
                # 回退到标题关键词
                ep_words = set(ep.get("title", "").lower().split())
                other_words = set(other.get("title", "").lower().split())
                common = ep_words & other_words - {"的", "了", "一个", "the", "a", "is", "of"}
                if len(common) >= 2:
                    cluster.append(other)
                    used.add(j)
                continue

            overlap = len(ep_entities & other_entities) / max(len(ep_entities | other_entities), 1)
            if overlap > 0.3:
                cluster.append(other)
                used.add(j)
                ep_entities |= other_entities

        if len(cluster) >= 2:
            clusters.append(cluster)

    return clusters


def _weave_narrative(cluster: list[dict]) -> dict | None:
    """将一组相关情景编织为叙事文本。

    叙事结构：
    1. 主题句：这段时间围绕 X 发生了什么
    2. 时间线：最早→最晚
    3. 因果推断：如果能识别出"发现→解决"模式
    4. 待解决问题：如果有 review_needed/低置信度记忆
    """
    if not cluster:
        return None

    sorted_eps = sorted(cluster, key=lambda x: x.get("created", ""))
    entities = _extract_common(cluster, "entities", limit=5)
    tags = _extract_common(cluster, "tags", limit=5)

    # 过滤噪音
    noise = {"error", "resolved", "source:hermes", "source:cli",
             "milestone", "breakthrough", "decision", "source:unknown"}
    meaningful = [e for e in entities if e.lower() not in noise]
    topic = meaningful[0] if meaningful else "general work"

    time_range = f"{sorted_eps[0].get('created','')[:10]} ~ {sorted_eps[-1].get('created','')[:10]}"
    title = f"[Narrative] {len(cluster)} events about {topic} ({time_range})"[:100]

    lines = [
        f"# 自我叙事：{topic}",
        f"",
        f"过去 24 小时内，围绕 **{topic}** 发生了 {len(cluster)} 个相关事件。",
        f"时间跨度：{time_range}",
    ]

    # 识别模式
    patterns = _detect_patterns(sorted_eps)
    if patterns:
        lines.append("")
        lines.append("## 事件模式")
        for p in patterns:
            lines.append(f"- {p}")

    # 时间线
    lines.append("")
    lines.append("## 时间线")
    for ep in sorted_eps:
        ts = ep.get("created", "")[:16]
        title_text = ep.get("title", "")[:80]
        # 标记重要事件
        mark = ""
        if ep.get("importance", 0) >= 0.85:
            mark = " ⚡"
        elif ep.get("confidence", 1) < 0.5:
            mark = " ⚠️"
        lines.append(f"- [{ts}]{mark} {title_text}")

    # 待解决问题
    open_issues = [
        ep for ep in sorted_eps
        if ep.get("review_needed") or ep.get("confidence", 1) < 0.5
    ]
    if open_issues:
        lines.append("")
        lines.append("## 待解决")
        for ep in open_issues:
            lines.append(f"- {ep.get('title','')[:80]} (置信度: {ep.get('confidence',0):.2f})")

    # 统计
    avg_importance = sum(e.get("importance", 0.5) for e in cluster) / len(cluster)
    sources = Counter(e.get("source", "unknown") for e in cluster)
    lines.append("")
    lines.append("## 统计")
    lines.append(f"- 平均重要性: {avg_importance:.2f}")
    lines.append(f"- 来源: {dict(sources.most_common(3))}")

    return {"title": title, "content": "\n".join(lines)}


def _detect_patterns(episodes: list[dict]) -> list[str]:
    """从事件序列中识别模式。"""
    patterns = []

    # 发现→解决模式
    has_discovery = any(
        any(kw in (ep.get("title", "") + ep.get("content", "")).lower()
            for kw in ["发现", "found", "bug", "失败", "错误", "error", "问题"])
        for ep in episodes
    )
    has_resolution = any(
        any(kw in (ep.get("title", "") + ep.get("content", "")).lower()
            for kw in ["解决", "修复", "fixed", "solved", "搞定", "完成"])
        for ep in episodes
    )
    if has_discovery and has_resolution:
        patterns.append("🔧 发现问题 → 解决问题：典型的调试/开发循环")

    # 配置变更模式
    has_config_change = any(
        any(kw in (ep.get("title", "") + ep.get("content", "")).lower()
            for kw in ["端口", "port", "配置", "config", "改成", "改为", "改用"])
        for ep in episodes
    )
    if has_config_change:
        patterns.append("⚙️ 系统配置调整：端口/参数发生变更")

    # 高密度事件
    if len(episodes) >= 5:
        patterns.append(f"📊 高密度活动：短时间内集中发生 {len(episodes)} 个事件")

    return patterns


def _extract_common(cluster: list[dict], field: str, limit: int = 8) -> list:
    """提取簇内共同字段。"""
    counter = Counter()
    for ep in cluster:
        items = json.loads(ep.get(field, "[]") or "[]")
        counter.update(items)
    return [item for item, _ in counter.most_common(limit)]