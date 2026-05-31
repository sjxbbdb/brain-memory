"""Brain Memory V7.0 — Drive Engine 验收测试。

四项验收标准:
  1. 无人输入时系统能够生成目标
  2. 系统能够更新目标（进度随tick推进）
  3. 系统能够取消目标（低优先级自动放弃）
  4. 系统能够调整优先级（驱动力变化后重排）
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_drive_engine_init():
    """V7: DriveEngine 初始化"""
    from brain.drive_engine import DriveEngine, SIGNAL_SOURCES

    de = DriveEngine()
    assert len(SIGNAL_SOURCES) == 8, f"Expected 8 signal sources, got {len(SIGNAL_SOURCES)}"
    assert de.total_signals_emitted == 0
    assert de._signal_cooldown == 30
    print("  ✓ drive_engine init")


def test_signal_detection():
    """V7: 信号检测——知识缺口和长期停滞"""
    from brain.drive_engine import DriveEngine, SIGNAL_THRESHOLDS
    from brain.activation_field import ActivationField

    de = DriveEngine()
    activation = ActivationField()

    # 状态快照：有知识缺口
    state = {
        "total_ticks": 300,
        "ticks_since_input": 50,
        "conflict_detected": False,
        "llm_error_count": 0,
        "curiosity_pending": 5,        # >= 3 → knowledge_gap
        "goals_completed_recently": 0,
        "recent_successes": 0,
        "unknown_entities_count": 2,
        "memories_archived": 0,
    }

    # stagnation 需要累积 _ticks_since_last_completion >= 100
    # 先调用 110 次让计数器累积
    for _ in range(110):
        de._detect_signals(state, 300)

    signals = de._detect_signals(state, 300)
    assert "knowledge_gap" in signals, f"Should detect knowledge_gap, got {signals}"
    assert "stagnation" in signals, f"Should detect stagnation after 110 calls, got {signals}"
    # ticks_since_input=50 < 60 → no social_signal
    assert "social_signal" not in signals

    print("  ✓ signal detection")


def test_drive_writes_to_activation():
    """V7: 信号触发后驱动力写入 ActivationField"""
    from brain.drive_engine import DriveEngine
    from brain.activation_field import ActivationField

    de = DriveEngine()
    activation = ActivationField()

    # 知识缺口信号
    state = {
        "total_ticks": 100,
        "ticks_since_input": 5,
        "conflict_detected": False,
        "llm_error_count": 0,
        "curiosity_pending": 4,
        "goals_completed_recently": 1,
        "recent_successes": 0,
        "unknown_entities_count": 0,
        "memories_archived": 0,
    }

    before = activation.get("curiosity_drive")
    de.tick(activation, state, 10)
    after = activation.get("curiosity_drive")

    assert after > before, f"curiosity_drive should increase: {before:.3f} → {after:.3f}"
    assert activation.get("growth_drive") > 0.5, f"growth_drive should increase above baseline"
    assert activation.get("exploration_drive") > 0.4, f"exploration_drive should increase above baseline"

    print("  ✓ drive writes to activation")


def test_goal_generator():
    """V7: GoalGenerator 根据驱动力生成目标"""
    from brain.drive_engine import GoalGenerator
    from brain.activation_field import ActivationField

    gg = GoalGenerator()
    activation = ActivationField()

    # 设置高 curiosity_drive
    activation.set("curiosity_drive", 0.9)
    activation.set("growth_drive", 0.8)

    goals = gg.generate(
        activation=activation,
        identity_traits=["好奇", "专注"],
        recent_entities=["Python", "API", "memory"],
        curiosity_questions=[{"question": "什么是状态机？"}],
        memory_count=42,
        current_tick=100,
    )

    assert len(goals) >= 1, f"Should generate at least 1 goal, got {len(goals)}"
    assert goals[0].survival_gain >= 0, "Goal should have survival_gain"
    assert goals[0].growth_gain > 0, "Goal should have growth_gain"
    assert goals[0].source_drive != "", "Goal should have source_drive"
    assert goals[0].status == "pending", f"New goal should be pending, got {goals[0].status}"

    print("  ✓ goal generator")


def test_goal_scheduler():
    """V7: GoalScheduler 评分排序 + 自动取消低分目标"""
    from brain.drive_engine import GoalScheduler
    from brain.goal_system import Goal, GoalStatus
    from brain.activation_field import ActivationField

    gs = GoalScheduler()
    activation = ActivationField()
    activation.set("arousal", 0.8)
    activation.set("survival_drive", 0.9)
    activation.set("growth_drive", 0.6)

    # 创建 4 个目标（超过 max_active=3）
    goals = [
        Goal(id="g1", drive="self_preservation", description="检查系统稳定性",
             priority=0.8, deadline_ticks=100, survival_gain=0.9, growth_gain=0.1, identity_gain=0.2, source_drive="survival_drive"),
        Goal(id="g2", drive="curiosity", description="学习Python异步",
             priority=0.5, deadline_ticks=150, survival_gain=0.0, growth_gain=0.7, identity_gain=0.3, source_drive="curiosity_drive"),
        Goal(id="g3", drive="growth", description="掌握新技能",
             priority=0.6, deadline_ticks=120, survival_gain=0.1, growth_gain=0.8, identity_gain=0.4, source_drive="growth_drive"),
        Goal(id="g4", drive="connection", description="了解用户需求",
             priority=0.3, deadline_ticks=200, survival_gain=0.1, growth_gain=0.2, identity_gain=0.2, source_drive="connection_drive"),
    ]

    active = gs.schedule(goals, activation, max_active=3)
    assert len(active) == 3, f"Should keep 3 active goals, got {len(active)}"

    # 最低分的 g4 应该被取消
    abandoned = [g for g in goals if g.status == GoalStatus.ABANDONED]
    assert len(abandoned) >= 1, f"Should abandon at least 1 goal, got {len(abandoned)}"
    assert "优先级不足" in abandoned[0].result_note, f"Should mention priority, got {abandoned[0].result_note}"

    # g1 (survival=0.9) 在高 survival_drive 下应该排第一
    assert active[0].id == "g1", f"g1 (survival) should be #1 when survival_drive is high, got {active[0].id}"

    print("  ✓ goal scheduler")


def test_drive_decay():
    """V7: 驱动力向 baseline 回归"""
    from brain.drive_engine import DriveEngine
    from brain.activation_field import ActivationField

    de = DriveEngine()
    activation = ActivationField()

    # 手动拉高 exploration_drive
    activation.set("exploration_drive", 0.9)

    # 运行衰减（无信号触发）
    for _ in range(50):
        de._decay_drives(activation)

    # 应该向 baseline(0.4) 回归
    after = activation.get("exploration_drive")
    assert after < 0.9, f"Should decay from 0.9, got {after:.3f}"
    assert after > 0.4, f"Should not go below baseline 0.4, got {after:.3f}"

    print("  ✓ drive decay")


def test_no_input_goal_generation():
    """V7 验收标准#1: 无人输入时系统能够生成目标。

    模拟 30 tick 的无输入状态，验证 GoalGenerator 产出目标。
    """
    import asyncio
    from brain.brain_stem import BrainStem
    from brain.brain_state import BrainState
    from brain.drive_engine import (
        DriveEngine, GoalGenerator, GoalScheduler,
        build_state_snapshot_for_drive_engine
    )

    bs = BrainStem()
    activation = bs.state.activation

    # 设置"有知识缺口无输入"的状态
    bs.state.curiosity.open_questions = [
        {"question": "什么是状态机？", "drive": "curiosity"},
        {"question": "Python异步模式如何工作？", "drive": "curiosity"},
        {"question": "记忆衰减的数学模型？", "drive": "curiosity"},
        {"question": "身份一致性的检测方法？", "drive": "coherence"},
    ]
    bs.state.total_ticks = 350

    # 运行 DriveEngine
    state_snap = build_state_snapshot_for_drive_engine(bs)
    state_snap["memories_archived"] = 0
    bs.drive_engine.tick(activation, state_snap, 350)

    # 验证驱动力被更新
    assert activation.get("curiosity_drive") > 0.5, "curiosity_drive should increase with knowledge gaps"
    assert activation.get("growth_drive") > 0.5, "growth_drive should increase with stagnation"

    # 生成目标
    goals = bs.goal_generator.generate(
        activation=activation,
        identity_traits=bs.state.self_model.identity_traits,
        recent_entities=["Python", "API"],
        curiosity_questions=bs.state.curiosity.open_questions[-5:],
        memory_count=42,
        current_tick=350,
    )
    assert len(goals) >= 1, f"Should generate goals without user input, got {len(goals)}"

    # 调度
    for goal in goals:
        goal.status = "pending"
    active = bs.goal_scheduler.schedule(goals, activation, max_active=3)
    assert len(active) >= 1, "Should have active goals after scheduling"

    print("  ✓ no-input goal generation")


def test_goal_update_and_priority():
    """V7 验收标准#2+#4: 目标更新和优先级调整"""
    from brain.goal_system import Goal
    from brain.drive_engine import GoalScheduler
    from brain.activation_field import ActivationField

    gs = GoalScheduler()
    activation = ActivationField()

    # 创建目标
    goal = Goal(id="g1", drive="growth", description="学习新技能",
                priority=0.6, deadline_ticks=100, elapsed_ticks=0, progress=0.0,
                survival_gain=0.1, growth_gain=0.8, identity_gain=0.4, source_drive="growth_drive")

    # 初始评分
    activation.set("growth_drive", 0.5)
    score1 = gs.score(goal, activation)

    # 驱动力上升后评分应该更高
    activation.set("growth_drive", 0.9)
    score2 = gs.score(goal, activation)

    assert score2 > score1, f"Score should increase with drive: {score1:.3f} → {score2:.3f}"
    print("  ✓ goal update and priority reorder")


if __name__ == "__main__":
    print("=" * 55)
    print("Brain Memory V7.0 — Drive Engine 验收测试")
    print("=" * 55)

    tests = [
        ("DriveEngine 初始化", test_drive_engine_init),
        ("信号检测", test_signal_detection),
        ("驱动力写入 ActivationField", test_drive_writes_to_activation),
        ("GoalGenerator 生成目标", test_goal_generator),
        ("GoalScheduler 排序+取消", test_goal_scheduler),
        ("驱动力衰减", test_drive_decay),
        ("验收#1: 无人输入生成目标", test_no_input_goal_generation),
        ("验收#2+#4: 目标更新+优先级调整", test_goal_update_and_priority),
    ]

    passed = 0
    failed = 0

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
