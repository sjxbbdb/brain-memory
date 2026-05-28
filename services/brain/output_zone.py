"""输出区 — 多区检索、前额叶过滤、上下文聚合、格式化输出。

对应人脑：前额叶输出通路 + 运动皮层
职责：
  1. 接收检索查询
  2. 从存储区提取候选记忆（五维检索）
  3. 经前额叶过滤（目标/情绪匹配）
  4. 聚合为可注入的上下文块
  5. 判断 should_inject
"""
from models.schemas import RetrievalQuery
from services import retrieval as ret_svc


async def retrieve_and_format(
    query: str,
    current_goal: str | None = None,
    current_risk: str | None = None,
    top_k: int = 5,
    min_relevance: float = 0.15,
) -> dict:
    """输出区检索管线：存储区检索 → 前额叶过滤 → 格式化。

    Args:
        query: 检索查询
        current_goal: 当前目标（用于前额叶过滤）
        current_risk: 当前风险
        top_k: 返回条数
        min_relevance: 最低相关性阈值

    Returns:
        {
            "memories": [...],
            "high_relevance": [...],
            "context_block": str,
            "should_inject": bool,
            "region_trace": ["storage_zone", "prefrontal", "output_zone"],
        }
    """
    # 1. 存储区检索（五维认知检索）
    search_result = await ret_svc.cognitive_search(
        RetrievalQuery(
            query=query,
            current_goal=current_goal,
            current_risk=current_risk,
            top_k=top_k,
        )
    )
    results = search_result["results"]

    # 2. 前额叶过滤：按目标相关度和情绪权重二次筛选
    if current_goal:
        goal_keywords = set(current_goal.lower().split())
        for r in results:
            mem_tags = set(t.lower() for t in r.memory.tags)
            mem_entities = set(e.lower() for e in r.memory.entities)
            goal_hits = sum(1 for kw in goal_keywords if kw in mem_tags or kw in mem_entities or any(kw in e for e in mem_entities))
            if goal_hits >= 1:
                r.score = min(1.0, r.score * 1.15)  # 目标匹配加成

        results.sort(key=lambda x: x.score, reverse=True)

    # 3. 格式化输出
    high_relevance = []
    context_lines = []

    for r in results:
        mem = r.memory
        entry = {
            "id": mem.id,
            "type": mem.type,
            "title": mem.title,
            "content": mem.content[:300],
            "score": round(r.score, 3),
            "emotion_weight": round(mem.emotion_weight, 2),
            "overwrite_weight": round(mem.overwrite_weight, 2),
            "created": mem.created,
            "tags": mem.tags,
            "relevance": "high" if r.score >= min_relevance else "low",
        }

        if r.score >= min_relevance:
            high_relevance.append(entry)
            context_lines.append(
                f"[{mem.type}] {mem.title} "
                f"(相关度: {r.score:.2f}, 情绪: {mem.emotion_weight:.2f}, 覆写权值: {mem.overwrite_weight:.2f})"
                f"\n  {mem.content[:200]}"
            )

    context_block = ""
    if context_lines:
        context_block = "=== 相关记忆 (Brain Memory v3.0) ===\n" + "\n".join(context_lines)

    high_scores = [r.score for r in results if r.score >= min_relevance]
    should_inject = len(high_scores) >= 1 and max(high_scores) > 0.55

    return {
        "memories": [
            {
                "id": r.memory.id,
                "type": r.memory.type,
                "title": r.memory.title,
                "content": r.memory.content[:200],
                "score": round(r.score, 3),
                "emotion_weight": round(r.memory.emotion_weight, 2),
            }
            for r in results
        ],
        "high_relevance": high_relevance,
        "context_block": context_block,
        "should_inject": should_inject,
        "region_trace": ["storage_zone", "prefrontal", "output_zone"],
    }
