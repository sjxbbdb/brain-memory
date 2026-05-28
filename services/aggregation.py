"""Topic Aggregation -- cluster related episodic memories into semantic summaries.

Consolidation Phase 28: scans episodic memories from the last 30 days,
groups them by entity overlap, and generates semantic summary memories.
"""
import json
from datetime import datetime, timezone, timedelta


async def aggregate_episodic_clusters(db, min_cluster_size: int = 2) -> int:
    """Scan episodic memories from last 30 days, cluster by entity overlap,
    generate semantic summaries. Returns number of summaries generated."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cursor = await db.execute(
        "SELECT * FROM memories WHERE type = 'episodic' AND archived = 0 "
        "AND created >= ? AND consolidated = 0 ORDER BY created",
        (cutoff,)
    )
    episodes = [dict(r) for r in await cursor.fetchall()]
    
    if len(episodes) < min_cluster_size:
        return 0
    
    # Greedy clustering by entity overlap
    clusters = []
    used = set()
    
    for i, ep in enumerate(episodes):
        if i in used:
            continue
        cluster = [ep]
        used.add(i)
        ep_entities = set(json.loads(ep.get('entities', '[]') or '[]'))
        
        for j, other in enumerate(episodes):
            if j in used:
                continue
            other_entities = set(json.loads(other.get('entities', '[]') or '[]'))
            if not ep_entities or not other_entities:
                # Fall back to tag overlap
                ep_tags = set(json.loads(ep.get('tags', '[]') or '[]'))
                other_tags = set(json.loads(other.get('tags', '[]') or '[]'))
                if ep_tags and other_tags:
                    overlap = len(ep_tags & other_tags) / max(len(ep_tags | other_tags), 1)
                    if overlap > 0.5:
                        cluster.append(other)
                        used.add(j)
                continue
            
            overlap = len(ep_entities & other_entities) / max(len(ep_entities | other_entities), 1)
            if overlap > 0.4:
                cluster.append(other)
                used.add(j)
        
        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)
    
    generated = 0
    now = datetime.now(timezone.utc)
    
    for cluster in clusters:
        # Generate title from common entities/tags
        all_entities = set()
        all_tags = set()
        for ep in cluster:
            all_entities.update(json.loads(ep.get('entities', '[]') or '[]'))
            all_tags.update(json.loads(ep.get('tags', '[]') or '[]'))
        
        # Remove noise tags
        noise = {'error', 'resolved', 'decision', 'milestone', 'breakthrough', 
                 'source:hermes', 'source:cli', 'source:unknown'}
        meaningful = all_entities | (all_tags - noise)
        topic = ', '.join(sorted(meaningful)[:3]) if meaningful else 'topic'
        
        title = f"[Aggregated] {cluster[0]['title'][:50]}"
        if len(cluster) > 2:
            title = f"[Aggregated] {len(cluster)} events about {topic}"[:80]
        
        # Generate content: timeline summary
        lines = [f"{len(cluster)} related events from the past 30 days, auto-aggregated into a semantic summary."]
        lines.append(f"Common entities: {', '.join(sorted(all_entities)[:10]) or 'none'}")
        lines.append("")
        lines.append("Timeline:")
        for ep in sorted(cluster, key=lambda x: x['created']):
            ts = ep['created'][:10] if ep.get('created') else '?'
            lines.append(f"- [{ts}] {ep['title'][:80]}")
        
        content = '\n'.join(lines)
        
        # Average importance from cluster members
        avg_importance = sum(e.get('importance', 0.5) for e in cluster) / len(cluster)
        
        mem_id = f"semantic-{now.strftime('%Y-%m-%d')}-agg{generated+1:03d}"
        
        await db.execute(
            """INSERT INTO memories (id, type, layer, title, content, importance, emotion_weight, 
               source, tags, entities, created, access_count)
               VALUES (?, 'semantic', 'semantic', ?, ?, ?, ?, 'aggregation', ?, ?, ?, 0)""",
            (mem_id, title, content, round(avg_importance, 2), round(avg_importance * 0.8, 2),
             json.dumps(list(all_tags)[:8], ensure_ascii=False),
             json.dumps(list(all_entities)[:10], ensure_ascii=False),
             now.isoformat())
        )
        
        # Mark original episodic memories as consolidated
        for ep in cluster:
            consolidated_to = json.loads(ep.get('consolidated_to', '[]') or '[]')
            consolidated_to.append(mem_id)
            await db.execute(
                "UPDATE memories SET consolidated = 1, consolidated_to = ?, last_consolidated = ? WHERE id = ?",
                (json.dumps(consolidated_to), now.isoformat(), ep['id'])
            )
        
        generated += 1
    
    await db.commit()
    return generated
