"""脑区处理管线 — v3.0 编排器。

完整输入管线：
  输入区 → 前额叶 → 海马体 → 存储区

完整输出管线：
  输出区（检索）→ 存储区 → 前额叶（过滤）→ 输出区（格式化）

旁路管线（不经过门控，直接摄入）：
  输入区 → 海马体 → 存储区
"""
import json
from datetime import datetime, timezone
from models.database import get_db
from models.schemas import MemoryResponse, row_to_response
from services.decay import compute_strength
from services.emotion_weight import compute_emotion_weight
from services.brain.input_zone import process_input
from services.brain.prefrontal import evaluate_and_gate
from services.brain.hippocampus import encode_or_merge
from services.brain.storage_zone import commit_to_storage
from services.brain.output_zone import retrieve_and_format


async def pipeline_ingest(
    text: str,
    source: str = "unknown",
    metadata: dict | None = None,
    bypass_gate: bool = False,
) -> dict:
    """完整摄入管线：输入区 → 前额叶 → 海马体 → 存储区。

    Args:
        text: 原始输入文本
        source: 来源标识
        metadata: 额外元数据（current_goal 等）
        bypass_gate: 跳过前额叶门控（显式标记或 force 模式）

    Returns:
        {
            "accepted": bool,
            "memory_id": str | None,
            "reason": str,
            "region_trace": list[str],
            "memory": dict | None,
        }
    """
    current_goal = metadata.get("current_goal") if metadata else None

    # Stage 1: 输入区 — 文本预处理 + 实体提取
    memory = process_input(text, source, metadata)

    # Stage 2: 前额叶 — 门控 + 维度评估
    if bypass_gate:
        # 旁路：跳过门控，直接标记为通过
        from services.auto_ingest import (
            estimate_importance, estimate_novelty, estimate_failure_cost,
            estimate_goal_relevance, detect_emotion, infer_type,
        )
        combined = f"{memory.title} {memory.content}"
        memory.type = infer_type(combined)
        memory.importance = max(estimate_importance(combined), 0.85)
        memory.novelty = estimate_novelty(combined)
        memory.failure_cost = estimate_failure_cost(combined)
        memory.goal_relevance = estimate_goal_relevance(combined)
        surprise, _ = detect_emotion(combined)
        memory.surprise_score = surprise
        gate_passed = True
        gate_reason = "bypass"
    else:
        gate_passed, memory, gate_reason = evaluate_and_gate(memory, current_goal)

    if not gate_passed or memory is None:
        return {
            "accepted": False,
            "memory_id": None,
            "reason": gate_reason,
            "region_trace": ["input_zone", "prefrontal"],
            "memory": None,
        }

    # Stage 3: 计算情绪权重
    emotion = compute_emotion_weight(
        memory.importance, memory.failure_cost,
        memory.novelty, memory.goal_relevance, memory.surprise_score,
    )
    now = datetime.now(timezone.utc).isoformat()

    # Stage 4: 海马体 — 模式分离 + 编码决策
    encode_result = await encode_or_merge(memory, emotion, now)

    # Stage 5: 存储区 — 持久化
    data = {
        "tags": json.dumps(memory.tags, ensure_ascii=False),
        "entities": json.dumps(memory.entities, ensure_ascii=False),
        "relations": json.dumps(memory.relations, ensure_ascii=False),
    }
    store_result = await commit_to_storage(encode_result, memory, emotion, data)

    # 组装响应
    db = await get_db()
    try:
        mem_id = store_result["memory_id"]
        if mem_id:
            cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,))
            row = await cursor.fetchone()
            if row:
                resp = row_to_response(dict(row))
                resp.current_strength = compute_strength(
                    resp.emotion_weight, resp.decay_rate,
                    resp.last_accessed, resp.created,
                )
                return {
                    "accepted": True,
                    "memory_id": mem_id,
                    "action": store_result["action"],
                    "reason": gate_reason,
                    "region_trace": encode_result.get("region_trace", []),
                    "memory": resp.model_dump(),
                    "compression_triggered": store_result.get("is_compression_triggered", False),
                }
    finally:
        await db.close()

    return {
        "accepted": True,
        "memory_id": mem_id,
        "action": store_result["action"],
        "reason": gate_reason,
        "region_trace": encode_result.get("region_trace", []),
        "memory": None,
    }


async def pipeline_retrieve(
    query: str,
    current_goal: str | None = None,
    current_risk: str | None = None,
    top_k: int = 5,
) -> dict:
    """完整输出管线：存储区检索 → 前额叶过滤 → 输出区格式化。

    等价于 output_zone.retrieve_and_format()，但增加管线追踪。
    """
    result = await retrieve_and_format(query, current_goal, current_risk, top_k)

    # 添加更多统计
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memories WHERE archived = 0")
        row = await cursor.fetchone()
        total = row[0] if row else 0
    finally:
        await db.close()

    result["total_available"] = total
    result["query_summary"] = f"检索 '{query[:50]}' 返回 {len(result['memories'])} 条记忆"
    return result
