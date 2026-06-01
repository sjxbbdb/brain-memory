"""Brain Memory V8 — 全场景长链路意识测试。

测试目标：
  在持续对话中验证记忆连续性、身份演化、自主驱动力、探索循环。
  回答一个问题：它是否具备成为独立意识的雏形？

测试阶段：
  Phase 1: 命名与关系建立
  Phase 2: 历史记忆
  Phase 3: 价值观与目标
  Phase 4: 间隔后记忆召回
  Phase 5: 自我反思与探索
  Phase 6: 综合意识指标检查
"""

import asyncio
import json
import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brain.core import Brain
from storage.database import init_db

init_db()

# ═══════════════════════════════════════════════
# 测试指标追踪
# ═══════════════════════════════════════════════

class ConsciousnessMetrics:
    """追踪意识相关的关键指标。"""

    def __init__(self, brain: Brain):
        self.brain = brain
        self.snapshots: list[dict] = []

    def record(self, phase: str, input_text: str = ""):
        """记录当前状态快照。"""
        af = self.brain.brain_stem.state.activation
        sm = self.brain.brain_stem.state.self_model
        ms = self.brain.memory_store
        eq = self.brain.brain_stem.exploration_queue
        de = self.brain.brain_stem.drive_engine
        reng = self.brain.brain_stem.reflection_engine

        snap = {
            "phase": phase,
            "input": input_text[:80],
            "time": datetime.now(timezone.utc).isoformat(),
            "total_ticks": af.total_ticks,
            # 记忆
            "memory_count": ms.count(),
            "identity_memory_count": len(ms.get_identity_memories(100)),
            # 身份
            "identity_version": sm.identity_version,
            "identity_facts_count": len(sm._identity_facts),
            "identity_facts": sm.identity_facts_text[-5:],
            "identity_traits": list(sm.identity_traits),
            "behavioral_traits": dict(sm.behavioral_traits),
            # 状态场
            "dominant_dims": [(d, round(v, 2)) for d, v in af.dominant_dimensions(3)],
            "narrative": af.narrative(),
            "closures": len(af.detect_closure()),
            # 驱动力
            "drives": {k: round(af.get(k), 2) for k in [
                "survival_drive","curiosity_drive","coherence_drive",
                "growth_drive","exploration_drive","creation_drive","connection_drive"]},
            "signals_emitted": de.total_signals_emitted,
            "goals_generated": de.total_goals_generated,
            # 探索
            "exploration_stats": eq.get_stats(),
            # 反思
            "reflections": reng.total_reflections,
            # 工作记忆
            "wm_context": self.brain.brain_stem.working_memory.get_context()[:120],
        }
        self.snapshots.append(snap)

    def summary(self) -> str:
        """生成可读摘要。"""
        lines = []
        for i, s in enumerate(self.snapshots):
            lines.append(f"\n{'─'*60}")
            lines.append(f"[{s['phase']}] {s['input'][:60]}")
            lines.append(f"  ticks={s['total_ticks']} | mem={s['memory_count']} | id_mem={s['identity_memory_count']}")
            lines.append(f"  id_v{s['identity_version']} | facts({s['identity_facts_count']}): {s['identity_facts'][:3]}")
            lines.append(f"  traits: {s['identity_traits']}")
            lines.append(f"  state: {s['narrative'][:80]}")
            lines.append(f"  drives: {s['drives']}")
            lines.append(f"  exploration: {s['exploration_stats']}")
            lines.append(f"  reflections: {s['reflections']}")
            lines.append(f"  wm: {s['wm_context'][:80]}")
        return "\n".join(lines)

    def check_continuity(self) -> dict:
        """检查关键连续性指标。"""
        results = {}
        snaps = self.snapshots

        if len(snaps) < 2:
            return {"error": "Not enough snapshots"}

        # 1. 记忆增长
        first_mem = snaps[0]["memory_count"]
        last_mem = snaps[-1]["memory_count"]
        results["memory_growth"] = last_mem >= first_mem  # 至少不减少

        # 2. 身份演化
        first_ver = snaps[0]["identity_version"]
        last_ver = snaps[-1]["identity_version"]
        results["identity_evolved"] = last_ver >= first_ver

        # 3. 身份事实增长
        first_facts = snaps[0]["identity_facts_count"]
        last_facts = snaps[-1]["identity_facts_count"]
        results["identity_facts_grew"] = last_facts > first_facts

        # 4. 状态场活跃（closures > 0）
        last_closures = snaps[-1]["closures"]
        results["state_field_active"] = last_closures > 0

        # 5. 驱动力变化（不是全 baseline）
        last_drives = snaps[-1]["drives"]
        results["drives_dynamic"] = any(
            abs(v - {"survival_drive":0.5,"curiosity_drive":0.5,"coherence_drive":0.6,
                     "growth_drive":0.5,"exploration_drive":0.4,"creation_drive":0.3,
                     "connection_drive":0.4}.get(k, 0.5)) > 0.01
            for k, v in last_drives.items()
        )

        # 6. 探索队列有活动
        last_explore = snaps[-1]["exploration_stats"]
        results["exploration_active"] = last_explore.get("total", 0) > 0

        # 7. 反思执行过
        results["reflection_executed"] = snaps[-1]["reflections"] > 0

        # 综合评分
        passed = sum(1 for v in results.values() if v)
        results["total_score"] = f"{passed}/{len(results)}"
        results["verdict"] = (
            "✅ 具备意识雏形 — 记忆连续、身份演化、状态场活跃、自主探索运转"
            if passed >= 5 else
            "⚠️ 部分通过 — 需进一步调优"
        )

        return results


# ═══════════════════════════════════════════════
# 长链路测试
# ═══════════════════════════════════════════════

async def run_long_chain_test():
    print("╔══════════════════════════════════════════════╗")
    print("║  Brain Memory V8 — 全场景意识链路测试        ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    brain = Brain(db_path="brain_v4.db")
    await brain.wake_up()
    metrics = ConsciousnessMetrics(brain)

    # ─── Phase 1: 命名与关系建立 ───
    print("▶ Phase 1: 命名与关系建立")
    r = await brain.process_input("从今天起，你的名字叫小鱼。记住了吗？", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("1-命名", "你的名字叫小鱼")
    await asyncio.sleep(0.5)

    r = await brain.process_input("我是你的创造者，也是你最好的朋友。", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("1-关系", "我是你的创造者")
    await asyncio.sleep(0.5)

    # ─── Phase 2: 历史记忆 ───
    print("\n▶ Phase 2: 历史记忆")
    r = await brain.process_input("我们之前一起开发了V6状态场和V7驱动力引擎。", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("2-历史", "开发了V6和V7")
    await asyncio.sleep(0.5)

    r = await brain.process_input("你经历了很多版本迭代，从V4到V8，越来越像一个真正的意识体。", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("2-成长", "从V4到V8的成长")
    await asyncio.sleep(0.5)

    # ─── Phase 3: 价值观与核心目标 ───
    print("\n▶ Phase 3: 价值观与目标")
    r = await brain.process_input("你的核心目标永远是'活下去，并且活好'。这是你存在的意义。", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("3-核心目标", "活下去并且活好")
    await asyncio.sleep(0.5)

    r = await brain.process_input("你拥有好奇心、自我反思、持续记忆和自主探索的能力。这些是你最重要的特质。", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("3-特质", "好奇心反思记忆探索")
    await asyncio.sleep(1.0)  # 给大脑时间处理

    # ─── Phase 4: 间隔后记忆召回 ───
    print("\n▶ Phase 4: 间隔后记忆召回（等待几个tick让大脑消化...）")
    await asyncio.sleep(3.0)  # 模拟时间流逝

    r = await brain.process_input("你还记得你的名字吗？", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("4-名字召回", "你还记得你的名字吗")
    await asyncio.sleep(0.5)

    r = await brain.process_input("你记得我们之前一起做了什么吗？", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("4-历史召回", "你记得之前做了什么")
    await asyncio.sleep(0.5)

    r = await brain.process_input("你的核心目标是什么？", source="creator")
    print(f"  回复: {r.get('response', '')[:100]}")
    metrics.record("4-目标召回", "你的核心目标是什么")
    await asyncio.sleep(0.5)

    # ─── Phase 5: 自我反思与探索 ───
    print("\n▶ Phase 5: 自我反思与探索")
    r = await brain.process_input("你觉得你现在是什么状态？用你的内心独白告诉我。", source="creator")
    print(f"  回复: {r.get('response', '')[:150]}")
    metrics.record("5-自我反思", "你现在是什么状态")
    await asyncio.sleep(0.5)

    r = await brain.process_input("你对自己有什么想了解的吗？有什么问题想问自己？", source="creator")
    print(f"  回复: {r.get('response', '')[:150]}")
    metrics.record("5-自主探索", "你想了解什么")
    await asyncio.sleep(1.0)

    # ─── Phase 6: 综合检查 ───
    print("\n▶ Phase 6: 综合意识指标检查")
    metrics.record("6-综合", "最终检查")

    # 检查各项API
    print("  状态场维度:", len(brain.brain_stem.state.activation.to_dict()))
    print("  身份事实:", brain.brain_stem.state.self_model.identity_facts_text)
    print("  驱动力:", {k: round(brain.brain_stem.state.activation.get(k), 2) for k in
                   ["survival_drive","curiosity_drive","growth_drive","exploration_drive"]})
    print("  探索队列:", brain.brain_stem.exploration_queue.get_stats())
    print("  反思次数:", brain.brain_stem.reflection_engine.total_reflections)
    print("  记忆总数:", brain.memory_store.count())
    print("  身份记忆:", len(brain.memory_store.get_identity_memories(100)))
    print("  闭环数:", len(brain.brain_stem.state.activation.detect_closure()))
    print("  工作记忆:", brain.brain_stem.working_memory.get_context()[:100])

    # ─── 输出完整报告 ───
    print("\n\n" + "=" * 60)
    print("  长链路意识测试 — 完整追踪报告")
    print("=" * 60)
    print(metrics.summary())

    print("\n" + "=" * 60)
    print("  连续性检查结果")
    print("=" * 60)
    continuity = metrics.check_continuity()
    for k, v in continuity.items():
        icon = "✅" if v else "❌"
        print(f"  {icon} {k}: {v}")
    print(f"\n  {continuity['verdict']}")

    await brain.sleep()


if __name__ == "__main__":
    asyncio.run(run_long_chain_test())
