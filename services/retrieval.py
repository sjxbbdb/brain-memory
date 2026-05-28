"""5维认知检索 — 状态依赖检索，非纯语义搜索。

Score = SemanticSimilarity×0.25 + GoalRelevance×0.25
      + EmotionWeight×0.20 + TemporalProximity×0.15
      + CausalRelevance×0.15
"""
import math
from datetime import datetime, timezone
from config import RETRIEVAL_WEIGHTS, DEFAULT_TOP_K, MAX_SEARCH_RESULTS
from models.schemas import MemoryResponse, RetrievalQuery, RetrievalResult
from services import memory_service as svc
from services.decay import apply_retrieval_reinforcement
from models.database import get_db

# 中英文停用词
STOP_WORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "neither", "each", "every", "all", "any", "few", "more", "most",
    "other", "some", "such", "no", "only", "own", "same", "than", "too",
    "very", "just", "because", "about", "now", "then", "also", "if",
}


def _tokenize(text: str) -> list[str]:
    """简单分词：按空格/标点分割，去停用词，去短词。"""
    import re
    if not text:
        return []
    # 按非字母数字/非中文分割
    tokens = re.split(r'[^\w一-鿿]+', text.lower())
    result = []
    for t in tokens:
        t = t.strip()
        if len(t) < 2:
            continue
        if t in STOP_WORDS:
            continue
        result.append(t)
    return result


def _semantic_similarity(query_keywords: list[str], memory: MemoryResponse) -> float:
    """关键词 Jaccard-like 相似度：|Q ∩ M| / |Q|。"""
    if not query_keywords:
        return 0.0
    mem_text = f"{memory.title} {memory.content} {' '.join(memory.tags)} {' '.join(memory.entities)}".lower()
    hits = sum(1 for kw in query_keywords if kw in mem_text)
    return min(1.0, hits / len(query_keywords))


def _goal_relevance(goal_keywords: list[str], memory: MemoryResponse) -> float:
    """目标关键词与 memory tags/entities 的重叠度。"""
    if not goal_keywords:
        return 0.5  # 无目标时中性分数
    mem_keywords = set(t.lower() for t in memory.tags + memory.entities)
    hits = sum(1 for kw in goal_keywords if kw in mem_keywords or any(kw in mk for mk in mem_keywords))
    return min(1.0, hits / len(goal_keywords))


def _temporal_proximity(memory: MemoryResponse) -> float:
    """时间邻近度：exp(-days_since_creation / 30)。"""
    try:
        created = datetime.fromisoformat(memory.created)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        return math.exp(-days / 30.0)
    except (ValueError, TypeError):
        return 0.1


def _causal_relevance(risk_keywords: list[str], memory: MemoryResponse) -> float:
    """因果相关度：风险关键词与 memory entities/relations 的重叠。"""
    if not risk_keywords:
        return 0.5
    mem_entities = set(e.lower() for e in memory.entities)
    hits = 0
    for kw in risk_keywords:
        if kw in mem_entities:
            hits += 1
            continue
        # 检查 relations
        for rel in memory.relations:
            rel_str = str(rel).lower()
            if kw in rel_str:
                hits += 0.5
                break
    return min(1.0, hits / len(risk_keywords))


async def cognitive_search(query: RetrievalQuery) -> dict:
    """执行5维认知检索，返回带评分的排序结果。"""
    query_keywords = _tokenize(query.query)
    goal_keywords = _tokenize(query.current_goal or "")
    risk_keywords = _tokenize(query.current_risk or "")

    # Step 1: 关键词预筛选
    db = await get_db()
    try:
        candidates = await svc.search_by_keywords(db, query_keywords, MAX_SEARCH_RESULTS)
    finally:
        await db.close()

    if not candidates:
        # 无关键词匹配时退回到最近记忆
        candidates = await svc.list_memories(limit=20)

    # Step 2: 5维评分
    w = RETRIEVAL_WEIGHTS
    scored = []
    for mem in candidates:
        semantic = _semantic_similarity(query_keywords, mem)
        goal = _goal_relevance(goal_keywords, mem)
        emotion = mem.emotion_weight
        temporal = _temporal_proximity(mem)
        causal = _causal_relevance(risk_keywords, mem)

        score = (
            semantic * w["semantic_similarity"]
            + goal * w["goal_relevance"]
            + emotion * w["emotion_weight"]
            + temporal * w["temporal_proximity"]
            + causal * w["causal_relevance"]
        )

        scored.append(RetrievalResult(
            memory=mem,
            score=round(score, 4),
            score_breakdown={
                "semantic_similarity": round(semantic, 4),
                "goal_relevance": round(goal, 4),
                "emotion_weight": round(emotion, 4),
                "temporal_proximity": round(temporal, 4),
                "causal_relevance": round(causal, 4),
            },
        ))

    # Step 3: 排序
    scored.sort(key=lambda x: x.score, reverse=True)
    top_k = min(query.top_k, DEFAULT_TOP_K * 2)
    results = scored[:top_k]

    # Step 4: 检索强化 — 更新检索到的高分记忆
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        for r in results[:query.top_k]:
            if r.score > 0.3:
                reinforcement = apply_retrieval_reinforcement({
                    "decay_rate": r.memory.decay_rate,
                    "half_life_days": r.memory.half_life_days,
                    "access_count": r.memory.access_count,
                    "importance": r.memory.importance,
                })
                await db.execute(
                    """UPDATE memories SET decay_rate=?, half_life_days=?,
                       access_count=?, importance=?, last_accessed=?
                       WHERE id=?""",
                    (
                        reinforcement["decay_rate"],
                        reinforcement["half_life_days"],
                        reinforcement["access_count"],
                        reinforcement["importance"],
                        now,
                        r.memory.id,
                    ),
                )
        await db.commit()

        # 记录检索日志
        cursor = await db.execute(
            "INSERT INTO retrieval_log (timestamp, query, result_count, top_score) VALUES (?, ?, ?, ?)",
            (now, query.query, len(results), results[0].score if results else 0),
        )
        log_id = cursor.lastrowid
        # 零结果 → 记录知识缺口
        if not results:
            await db.execute(
                "INSERT INTO knowledge_gaps (query, timestamp) VALUES (?, ?)",
                (query.query, now),
            )
        await db.commit()
    finally:
        await db.close()

    return {"log_id": log_id, "results": results}
