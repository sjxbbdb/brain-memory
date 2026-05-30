"""睡眠巩固引擎 — Phase 0-3 实现。

Phase 0: 权重自适应
Phase 1: 评分刷新
Phase 2: 分诊清理
Phase 2.5: 冲突仲裁
Phase 3: 记忆修正（规则化）
"""
import json
import math
from datetime import datetime, timezone
from config import (
    EMOTION_WEIGHTS,
    EVICTION_THRESHOLD,
    STALENESS_THRESHOLD,
    MAX_REVISIONS,
)
from models.database import get_db
from models.schemas import ConsolidationReport, PhaseResult
from services.emotion_weight import compute_emotion_weight
from services.decay import compute_staleness

# source 优先级映射 (用于冲突仲裁)
SOURCE_PRIORITY = {
    "direct_experience": 4,
    "observed": 3,
    "inferred": 2,
    "told_by": 1,
}


async def run_consolidation(phases: list[int] | None = None) -> ConsolidationReport:
    """执行巩固流程。phases 默认 [0, 1, 2, 25, 3]"""
    if phases is None:
        phases = [0, 1, 2, 25, 3]

    now = datetime.now(timezone.utc).isoformat()
    phase_results: dict[str, list[PhaseResult]] = {}
    total_changes = 0
    executed = []

    phase_map = {
        0: ("phase0", _phase0_weight_adaptation),
        1: ("phase1", _phase1_scoring),
        2: ("phase2", _phase2_triage),
        25: ("phase25", _phase25_conflict_resolution),
        28: ("phase28", _phase28_aggregation),
        3: ("phase3", _phase3_revision),
    }

    for p in phases:
        if p in phase_map:
            name, func = phase_map[p]
            results = await func()
            phase_results[name] = results
            for r in results:
                total_changes += r.affected_count
            executed.append(name)

    # 记录巩固日志
    db = await get_db()
    try:
        for phase_name, results in phase_results.items():
            for r in results:
                await db.execute(
                    "INSERT INTO consolidation_log (timestamp, phase, action, details, affected_count) VALUES (?, ?, ?, ?, ?)",
                    (now, phase_name, r.action, json.dumps({"summary": str(r)}), r.affected_count),
                )
        await db.commit()
    finally:
        await db.close()

    return ConsolidationReport(
        timestamp=now,
        phases_executed=executed,
        phase_results={k: v for k, v in phase_results.items()},
        memory_changes=total_changes,
    )


async def _phase0_weight_adaptation() -> list[PhaseResult]:
    """根据 retrieval_log 反馈自适应调整评分因子权重。学习率 0.01。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM retrieval_log WHERE feedback != 0"
        )
        row = await cursor.fetchone()
        feedback_count = row[0]
        if feedback_count < 10:
            return [PhaseResult(action="phase0_skip", affected_count=0)]

        # 简化版：根据平均 top_score 趋势微调 novelty 和 temporal 权重
        cursor = await db.execute(
            "SELECT AVG(top_score) FROM retrieval_log WHERE feedback > 0"
        )
        avg_positive = (await cursor.fetchone())[0] or 0.5
        cursor = await db.execute(
            "SELECT AVG(top_score) FROM retrieval_log WHERE feedback < 0"
        )
        avg_negative = (await cursor.fetchone())[0] or 0.3

        delta = avg_positive - avg_negative
        if delta > 0.2:
            # 正向反馈好 → 微调 temporal 权重
            # NOTE: weight adaptation logged but not persisted (survives only this process lifetime)\n            EMOTION_WEIGHTS["goal_relevance"] = round(EMOTION_WEIGHTS["goal_relevance"] + 0.01, 4)
            EMOTION_WEIGHTS["surprise_score"] = round(EMOTION_WEIGHTS["surprise_score"] - 0.01, 4)
            return [PhaseResult(action="weight_adjusted", affected_count=2)]
        return [PhaseResult(action="phase0_no_change", affected_count=0)]
    finally:
        await db.close()


async def _phase1_scoring() -> list[PhaseResult]:
    """刷新所有未归档记忆的 emotion_weight 和 staleness。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0"
        )
        rows = await cursor.fetchall()
        updated = 0
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            r = dict(row)
            new_emotion = compute_emotion_weight(
                r["importance"], r["failure_cost"],
                r["novelty"], r["goal_relevance"], r["surprise_score"],
            )
            if abs(new_emotion - r["emotion_weight"]) > 0.001:
                await db.execute(
                    "UPDATE memories SET emotion_weight = ? WHERE id = ?",
                    (round(new_emotion, 4), r["id"]),
                )
                updated += 1

        await db.commit()
        return [PhaseResult(action="emotion_refreshed", affected_count=updated)]
    finally:
        await db.close()


async def _phase2_triage() -> list[PhaseResult]:
    """分诊清理：低权归档，陈旧标记审查。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0"
        )
        rows = await cursor.fetchall()
        archived_count = 0
        review_count = 0
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            r = dict(row)
            emotion = r["emotion_weight"]

            # 低权清理
            if emotion < EVICTION_THRESHOLD:
                if r["type"] == "episodic":
                    await db.execute(
                        "UPDATE memories SET archived = 1, last_consolidated = ? WHERE id = ?",
                        (now, r["id"]),
                    )
                    archived_count += 1
                elif r["type"] == "semantic":
                    await db.execute(
                        "UPDATE memories SET confidence = confidence * 0.8 WHERE id = ?",
                        (r["id"],),
                    )
                    review_count += 1
                # procedural 不清理
                continue

            # 陈旧检查
            staleness = compute_staleness(r["last_accessed"], r["created"])
            if staleness > STALENESS_THRESHOLD and r["access_count"] < 2:
                new_conf = round(r["confidence"] * 0.7, 4)
                await db.execute(
                    "UPDATE memories SET confidence = ?, review_needed = 1, last_consolidated = ? WHERE id = ?",
                    (new_conf, now, r["id"]),
                )
                review_count += 1

        await db.commit()
        return [
            PhaseResult(action="archived", affected_count=archived_count),
            PhaseResult(action="review_marked", affected_count=review_count),
        ]
    finally:
        await db.close()


async def _phase25_conflict_resolution() -> list[PhaseResult]:
    """冲突检测与仲裁：entities 重叠但结论矛盾 → source优先级仲裁。"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0"
        )
        rows = await cursor.fetchall()
        if len(rows) < 2:
            return [PhaseResult(action="no_conflicts", affected_count=0)]

        resolved = 0
        memories = [dict(r) for r in rows]

        for i in range(len(memories)):
            for j in range(i + 1, len(memories)):
                a, b = memories[i], memories[j]
                entities_a = set(json.loads(a["entities"] or "[]"))
                entities_b = set(json.loads(b["entities"] or "[]"))
                overlap = entities_a & entities_b

                # 实体重叠 > 50%
                min_len = min(len(entities_a), len(entities_b))
                if min_len == 0 or len(overlap) / max(min_len, 1) < 0.5:
                    continue

                # 内容矛盾检测：至少一方包含对立/修正关键词
                combined = f"{a['title']} {a['content']} {b['title']} {b['content']}".lower()
                contradiction_markers = [
                    "但是", "然而", "不同", "错误", "修正", "纠正", "推翻",
                    "不对", "不能", "并非", "actually", "wrong",
                    "correct", "correction", "contradict", "however"
                ]
                has_contradiction = any(m in combined for m in contradiction_markers)
                if not has_contradiction:
                    continue  # 实体重叠但内容不矛盾，跳过
                priority_a = SOURCE_PRIORITY.get(a["source"], 1)
                priority_b = SOURCE_PRIORITY.get(b["source"], 1)

                if priority_a == priority_b:
                    # 同优先级：confidence 高者胜
                    if a["confidence"] == b["confidence"]:
                        # 时间近者胜
                        winner, loser = (a, b) if a["created"] > b["created"] else (b, a)
                    else:
                        winner, loser = (a, b) if a["confidence"] > b["confidence"] else (b, a)
                else:
                    winner, loser = (a, b) if priority_a > priority_b else (b, a)

                # 更新败者
                contradicted = json.loads(loser.get("contradicted_by") or "[]")
                if winner["id"] not in contradicted:
                    contradicted.append(winner["id"])
                new_conf = round(loser["confidence"] * 0.3, 4)
                await db.execute(
                    "UPDATE memories SET confidence = ?, contradicted_by = ? WHERE id = ?",
                    (new_conf, json.dumps(contradicted), loser["id"]),
                )
                resolved += 1

        await db.commit()
        return [PhaseResult(action="conflicts_resolved", affected_count=resolved)]
    finally:
        await db.close()




async def _phase28_aggregation() -> list[PhaseResult]:
    """Phase 28: Cluster episodic memories and generate semantic summaries."""
    from services.aggregation import aggregate_episodic_clusters
    db = await get_db()
    try:
        count = await aggregate_episodic_clusters(db, min_cluster_size=2)
        if count > 0:
            return [PhaseResult(action="clusters_aggregated", affected_count=count)]
        return [PhaseResult(action="no_clusters_found", affected_count=0)]
    finally:
        await db.close()

async def _phase3_revision() -> list[PhaseResult]:
    """记忆修正：对低置信度 / review_needed 记忆进行规则化处理。

    无LLM时的规则：
    - confidence < 0.3 → 归档
    - 相似标题 (Jaccard > 0.8) → 标记合并建议（仅日志）
    - 过长内容 (> 2000 字符) → 建议压缩（仅日志）
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM memories WHERE archived = 0 AND (confidence < 0.5 OR review_needed = 1)"
        )
        rows = await cursor.fetchall()
        revised = 0
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            r = dict(row)

            # 极低置信度 → 归档
            if r["confidence"] < 0.3 and r["type"] == "episodic":
                await db.execute(
                    "UPDATE memories SET archived = 1, last_consolidated = ? WHERE id = ?",
                    (now, r["id"]),
                )
                revised += 1
                continue

            # 审查完成且置信度尚可 → 清除 review_needed
            if r["review_needed"] and r["confidence"] >= 0.5:
                await db.execute(
                    "UPDATE memories SET review_needed = 0, last_consolidated = ? WHERE id = ?",
                    (now, r["id"]),
                )
                revised += 1

        await db.commit()

        # 标记所有处理过的记忆为已巩固
        await db.execute(
            "UPDATE memories SET consolidated = 1, last_consolidated = ? WHERE archived = 0 AND consolidated = 0",
            (now,),
        )
        await db.commit()

        return [PhaseResult(action="revised", affected_count=revised)]
    finally:
        await db.close()
