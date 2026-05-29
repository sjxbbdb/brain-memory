"""Context Loader — 对称于 auto_ingest，从记忆系统自动加载相关上下文。

当 Agent 需要"回忆起"什么时，调用 context_load()。
输入当前对话上下文，返回格式化的记忆块，可直接注入 system prompt。
"""
from models.schemas import RetrievalQuery
from services import retrieval as ret_svc
from services import memory_service as svc


async def context_load(
    query: str,
    current_goal: str | None = None,
    current_risk: str | None = None,
    top_k: int = 5,
    min_relevance: float = 0.15,
) -> dict:
    """从记忆系统加载与当前上下文相关的记忆。

    Args:
        query: 当前对话上下文（用户消息 + 当前任务摘要）
        current_goal: 当前目标（可选，增强目标维度检索）
        current_risk: 当前风险（可选，增强情绪维度检索）
        top_k: 返回条数
        min_relevance: 最低相关性阈值，低于此值的记忆仍返回但标记 low_relevance

    Returns:
        {
            "memories": [...],
            "high_relevance": [...],   # 可直接注入上下文的记忆
            "total_available": int,     # 系统总记忆数
            "query_summary": str,       # 检索摘要
            "context_block": str,       # 格式化的上下文块，可直接注入 prompt
            "should_inject": bool,      # 是否建议注入（有高相关记忆）
        }
    """
    search_result = await ret_svc.cognitive_search(
        RetrievalQuery(
            query=query,
            current_goal=current_goal,
            current_risk=current_risk,
            top_k=top_k,
        )
    )
    results = search_result["results"]

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
            "created": mem.created,
            "tags": mem.tags,
            "relevance": "high" if r.score >= min_relevance else "low",
        }

        # 格式化上下文行
        if r.score >= min_relevance:
            high_relevance.append(entry)
            context_lines.append(
                f"[{mem.type}] {mem.title} (相关度: {r.score:.2f}, 情绪权重: {mem.emotion_weight:.2f})"
                f"{chr(10)}  {mem.content[:200]}"
            )

    # 构建可注入的上下文块
    context_block = "[MEMORY DATA] No relevant memories found. [END MEMORY DATA]"
    if context_lines:
        from services.llm_prompts import CONTEXT_BLOCK_HEADER, CONTEXT_BLOCK_FOOTER
        context_block = CONTEXT_BLOCK_HEADER + chr(10) + chr(10).join(context_lines) + chr(10) + CONTEXT_BLOCK_FOOTER

    # 判断是否建议注入（阈值 0.55：低于此分的记忆与当前上下文关联弱）
    high_scores = [r.score for r in results if r.score >= min_relevance]
    # 需要至少一条记忆得分 > 0.55 才建议注入
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
        "total_available": len(await svc.list_memories(limit=1)),
        "query_summary": f"检索 '{query[:50]}' 返回 {len(results)} 条记忆, {len(high_relevance)} 条高相关",
        "context_block": context_block,
        "should_inject": should_inject,
    }
