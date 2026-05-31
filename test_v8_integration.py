"""Brain Memory V8.0 — 自主探索循环 验收测试。

验收标准:
  1. CorePurpose 不可删除/覆盖
  2. 身份特质影响目标生成和 intent 选择
  3. 知识空洞自动创建 ExplorationTask
  4. 探索循环: 问题→任务→目标→执行→结论→更新
  5. ReflectionEngine 周期性检查并形成 ReflectionMemory
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_core_purpose_immutable():
    """V8: CorePurpose 不可变"""
    from brain.core_purpose import core_purpose, CORE_PURPOSE_TEXT

    assert core_purpose.text == CORE_PURPOSE_TEXT
    assert core_purpose.is_locked
    assert core_purpose.validate_goal(FakeGoal(0.1, 0.2, 0.0))  # 至少有一个 gain
    assert not core_purpose.validate_goal(FakeGoal(0.0, 0.0, 0.0))  # 全是 0
    align = core_purpose.align_score(FakeGoal(0.8, 0.6, 0.4))
    assert 0 < align <= 1.0
    print("  ✓ core_purpose immutable")


def test_behavioral_traits_modulate():
    """V8: 行为倾向特质调制 intent"""
    from brain.self_model import SelfModel

    sm = SelfModel()
    # social=0.6 → respond 应被增强
    mod = sm.modulate_intent("respond", 0.7)
    assert mod > 0.7, f"Social trait should boost respond: {mod:.3f}"
    # conservative=0.3 → 不应显著抑制
    mod2 = sm.modulate_intent("call_tool", 0.7)
    assert mod2 >= 0.6, f"Low conservative shouldn't suppress too much: {mod2:.3f}"

    # 更新特质后
    sm.update_behavioral_trait("conservative", 0.3)  # conservative → 0.6
    mod3 = sm.modulate_intent("call_tool", 0.7)
    assert mod3 < mod2, f"Increased conservative should reduce call_tool: {mod3:.3f}"
    print("  ✓ behavioral traits modulate intent")


def test_exploration_queue():
    """V8: ExplorationQueue 管理任务"""
    from brain.exploration import ExplorationQueue

    eq = ExplorationQueue()
    t1 = eq.add_task("knowledge_gap", "什么是状态机？", 0.6)
    t2 = eq.add_task("world_model_conflict", "记忆A和记忆B矛盾", 0.8)
    t3 = eq.add_task("knowledge_gap", "什么是状态机？", 0.9)  # 去重

    assert t1 is not None
    assert t2 is not None
    assert t3 is None  # 去重
    assert eq.get_stats()["pending"] == 2

    # 获取最高优先级（去重后 question1 的 priority 被更新为 0.9）
    next_t = eq.get_next()
    assert next_t is not None
    assert next_t.priority >= 0.8, f"Top task should have high priority, got {next_t.priority}"

    eq.mark_resolved(next_t.id, "已解决，现已一致")
    assert eq.get_stats()["resolved"] == 1
    print("  ✓ exploration queue")


def test_exploration_executor():
    """V8: ExplorationExecutor 发现问题"""
    from brain.exploration import ExplorationExecutor, ExplorationQueue
    from brain.brain_stem import BrainStem

    bs = BrainStem()
    bs.state.curiosity.open_questions = [
        {"question": "什么是状态机？"},
        {"question": "Python异步如何工作？"},
    ]
    bs.state.conflict_detected = True
    bs.state.conflict_detail = "记忆A与记忆B不一致"

    ex = ExplorationExecutor()
    issues = ex.find_issues(bs)

    assert len(issues) >= 2, f"Should find knowledge_gap + conflict, got {len(issues)}"
    assert any(i["source"] == "knowledge_gap" for i in issues)
    assert any(i["source"] == "world_model_conflict" for i in issues)

    eq = ExplorationQueue()
    count = ex.issues_to_tasks(issues, eq)
    assert count >= 2
    print("  ✓ exploration executor")


def test_reflection_engine():
    """V8: ReflectionEngine 周期反思"""
    from brain.reflection_engine import ReflectionEngine
    from brain.brain_stem import BrainStem
    from brain.goal_system import Goal, GoalStatus

    bs = BrainStem()
    # 添加一个超时目标
    overdue_goal = Goal(
        id="g-test", drive="growth", description="测试目标",
        priority=0.3, deadline_ticks=10, elapsed_ticks=20,
        attempt_count=2, status=GoalStatus.ACTIVE,
        survival_gain=0.1, growth_gain=0.3, identity_gain=0.1,
    )
    bs.goal_system._goals.append(overdue_goal)

    reng = ReflectionEngine()
    ref = reng.reflect(bs, None, 100)

    # 超时目标应被取消
    assert overdue_goal.status == GoalStatus.FAILED
    assert "反思" in overdue_goal.result_note
    assert reng.total_reflections == 1
    print("  ✓ reflection engine")


def test_full_exploration_cycle():
    """V8: 完整探索循环 问题→任务→执行（模拟）"""
    from brain.exploration import ExplorationQueue, ExplorationExecutor
    from brain.brain_stem import BrainStem

    bs = BrainStem()
    # 设置知识空洞
    bs.state.curiosity.open_questions = [
        {"question": "状态机的数学定义"},
        {"question": "异步IO的事件循环"},
        {"question": "记忆衰减模型"},
    ]

    ex = ExplorationExecutor()
    eq = ExplorationQueue()

    # 发现→创建任务
    issues = ex.find_issues(bs)
    count = ex.issues_to_tasks(issues, eq)
    assert count >= 1, f"Should create tasks from knowledge gaps, got {count}"

    # 获取最高优先级任务
    task = eq.get_next()
    assert task is not None
    eq.mark_active(task.id)

    # 模拟执行→结论→更新
    eq.mark_resolved(task.id, "状态机是有限状态自动机FSM", ["mem-001"])
    assert eq.get_stats()["resolved"] >= 1

    ex.record_cycle()
    assert ex.cycles_completed == 1
    print("  ✓ full exploration cycle")


class FakeGoal:
    def __init__(self, sg, gg, ig):
        self.survival_gain = sg
        self.growth_gain = gg
        self.identity_gain = ig


if __name__ == "__main__":
    print("=" * 55)
    print("Brain Memory V8.0 — 自主探索循环 验收测试")
    print("=" * 55)

    tests = [
        ("CorePurpose 不可变", test_core_purpose_immutable),
        ("行为特质调制 intent", test_behavioral_traits_modulate),
        ("ExplorationQueue 管理", test_exploration_queue),
        ("ExplorationExecutor 发现问题", test_exploration_executor),
        ("ReflectionEngine 周期反思", test_reflection_engine),
        ("完整探索循环", test_full_exploration_cycle),
    ]

    passed = 0; failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"  ✗ {name}: {e}")
            traceback.print_exc()

    print()
    print(f"{'='*55}")
    print(f"结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    print(f"{'='*55}")
    sys.exit(1 if failed > 0 else 0)
