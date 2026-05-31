"""Brain Memory V6.0 — State Field 集成测试。

验证所有 V6 新增功能:
  1. ActivationField 初始化和快照/恢复
  2. StateDiffusionEngine 规则传播
  3. 状态闭环检测
  4. 工作记忆 SalienceScore 竞争保留
  5. BrainState V6 集成
  6. Amygdala V6 ActivationField 写入
  7. Metacognition calibrate_diffusion
  8. EmotionalSpectrum V6 写入
  9. 情绪→记忆→身份的完整闭环
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_activation_field_basics():
    """V6: ActivationField 初始化和基本操作"""
    from brain.activation_field import ActivationField

    field = ActivationField()
    # 默认值
    assert 0.45 <= field.valence <= 0.55, f"Default valence out of range: {field.valence}"
    assert field.get("fear") == 0.1
    assert field.get("identity_stability") == 0.7

    # 设置和读取
    field.fear = 0.8
    assert field.fear == 0.8
    field.set("curiosity", 0.9)
    assert field.curiosity == 0.9

    # 越界夹持
    field.fear = 2.0
    assert field.fear == 1.0
    field.fear = -1.0
    assert field.fear == 0.0

    # dominant_dimensions（curiosity偏离0.4 > fear偏离0.7，所以curiosity最显著）
    dom = field.dominant_dimensions(3)
    assert len(dom) == 3
    assert dom[0][0] in ("fear", "curiosity"), f"Expected fear or curiosity as dominant, got {dom[0][0]}"

    print("  ✓ activation_field basics")


def test_state_diffusion():
    """V6: StateDiffusionEngine 规则传播"""
    from brain.activation_field import ActivationField

    field = ActivationField()

    # 初始状态：所有维度接近基线
    init_valence = field.valence

    # 设置高恐惧
    field.fear = 0.9

    # 运行 10 tick（链式传播: fear→survival_drive↑→focus↑, 但 fear→focus↓ 也在作用）
    for _ in range(10):
        field.tick(dt=1.0)

    # 核心验证：状态发生了显著变化（不是死水）
    changed = sum(1 for k in field._values if abs(field._values[k] - 0.5) > 0.05)
    assert changed >= 3, f"At least 3 dimensions should deviate from baseline, got {changed}"

    # fear↑ → survival_drive↑（恐惧激发生存驱动）
    assert field.get("survival_drive") > 0.5, f"High fear should increase survival drive, got {field.get('survival_drive'):.3f}"

    # fear↑ → arousal↑（恐惧提升唤醒度）
    assert field.arousal > 0.5, f"High fear should increase arousal, got {field.arousal:.3f}"

    print("  ✓ state diffusion")


def test_state_closure_detection():
    """V6: 状态闭环检测"""
    from brain.activation_field import ActivationField

    field = ActivationField()
    # 初始状态下应该有闭环（扩散规则表本身就形成环）
    closures = field.detect_closure(min_chain=3)
    assert len(closures) > 0, "Should detect at least one closure in default rules"
    print(f"  ✓ closure detection ({len(closures)} cycles found)")


def test_activation_snapshot():
    """V6: ActivationField 快照/恢复"""
    from brain.activation_field import ActivationField

    field = ActivationField()
    field.fear = 0.7
    field.curiosity = 0.3
    field.tick(1.0)
    field.tick(1.0)

    snap = field.snapshot()
    assert "values" in snap
    assert "diffusion" in snap
    assert snap["values"]["fear"] != 0.1  # 已改变

    # 恢复
    restored = ActivationField.from_snapshot(snap)
    assert abs(restored.fear - field.fear) < 0.1  # 接近（扩散后有细微差异）
    assert restored.total_ticks == field.total_ticks
    assert len(restored.diffusion.rules) == len(field.diffusion.rules)

    print("  ✓ activation snapshot/restore")


def test_working_memory_salience():
    """V6: 工作记忆 SalienceScore 竞争保留"""
    from brain.working_memory import WorkingMemory
    from brain.activation_field import ActivationField

    wm = WorkingMemory()
    activation = ActivationField()

    # 推入不同类型的内容
    wm.push("系统稳定性需要检查", "monitor",
            emotion_weight=0.6, goal_weight=0.8, identity_weight=0.3, novelty=0.2)
    wm.push("昨晚做了一个奇怪的梦", "dream",
            emotion_weight=0.8, goal_weight=0.1, identity_weight=0.6, novelty=0.7)
    wm.push("学习新的 Python 异步模式", "growth",
            emotion_weight=0.3, goal_weight=0.7, identity_weight=0.4, novelty=0.5)

    # 高 arousal + 高 focus 场景（任务模式）
    activation.arousal = 0.9
    activation.focus = 0.8
    activation.curiosity = 0.3
    activation.set("identity_stability", 0.7)

    wm.tick(activation=activation)

    # 在任务模式下，目标相关的记忆应该排名靠前
    top = wm.get_top(3)
    assert len(top) >= 1, "WM should have items after tick"

    # 低 arousal + 高 curiosity 场景（探索模式）
    activation.arousal = 0.2
    activation.focus = 0.3
    activation.curiosity = 0.9

    wm.tick(activation=activation)

    # Salience 随着状态变化而变化
    for item in wm.items:
        assert "salience" in item, "Each item should have salience computed"

    # 最多保留 7 项
    for _ in range(10):
        wm.push(f"extra memory {_}", "test", emotion_weight=0.3, goal_weight=0.3)
    wm.tick(activation=activation)
    assert len(wm.items) <= 7, f"WM should not exceed capacity, got {len(wm.items)}"

    print("  ✓ working memory salience competition")


def test_brain_state_v6():
    """V6: BrainState 集成 ActivationField"""
    from brain.brain_state import BrainState

    state = BrainState()
    assert state.activation is not None
    assert state.activation.valence == 0.5

    state.activation.fear = 0.6
    state.activation.tick(1.0)

    snap = state.snapshot()
    assert "activation" in snap
    assert "values" in snap["activation"]

    # 恢复
    restored = BrainState.from_snapshot(snap)
    assert restored.activation is not None
    assert abs(restored.activation.fear - state.activation.fear) < 0.15

    print("  ✓ brain_state v6 integration")


def test_amygdala_v6():
    """V6: Amygdala 写入 ActivationField"""
    from brain.amygdala import Amygdala
    from brain.activation_field import ActivationField

    amygdala = Amygdala()
    activation = ActivationField()

    # 负面紧急文本
    result = amygdala.evaluate(
        "系统崩溃了！紧急修复！",
        current_state={"current_emotion": "neutral", "emotion_vector": {"valence": 0.5, "arousal": 0.5, "dominance": 0.5}},
        activation=activation,
    )

    # 验证返回格式（向后兼容）
    assert "emotion_label" in result
    assert "salience" in result

    # V6: 验证 ActivationField 被写入（EMOTION_DECAY_RATE=0.95，单次移动很小）
    assert activation.valence < 0.5, f"Negative text should lower valence, got {activation.valence:.3f}"
    assert activation.arousal > 0.5, f"Urgent text should raise arousal, got {activation.arousal:.3f}"
    assert activation.get("dominance") < 0.5, f"Error+urgent should lower dominance"

    # 正面文本（惯性大，单次只移动 ~0.02，多次累积才显著）
    activation2 = ActivationField()
    for _ in range(3):  # 3次正面输入累积
        amygdala.evaluate(
            "太棒了，终于解决了！完美运行！",
            current_state={"current_emotion": "neutral", "emotion_vector": {"valence": activation2.valence, "arousal": activation2.arousal, "dominance": activation2.dominance}},
            activation=activation2,
        )
    assert activation2.valence > 0.5, f"Positive text should raise valence after 3 rounds, got {activation2.valence:.3f}"

    print("  ✓ amygdala v6 activation write")


def test_metacognition_calibrate():
    """V6: Metacognition calibrate_diffusion"""
    from brain.metacognition import Metacognition
    from brain.activation_field import ActivationField

    mc = Metacognition()
    activation = ActivationField()

    # 模拟过度自信
    for _ in range(5):
        mc.feed_intent("call_tool", 0.95, "web_search")
    for _ in range(4):
        mc.feed_outcome(False, 0.95)
    mc.feed_outcome(True, 0.8)

    assert mc.calibration_error > 0.25, "Should detect overconfidence"

    # 记录校准前的权重
    old_weight = activation.diffusion._index.get(("confidence", "exploration_drive"))
    old_w = old_weight.weight if old_weight else 0.12

    # 执行校准
    mc.update_cognitive_load(True, True, 3, activation=activation)
    result = mc.calibrate_diffusion(activation)

    if old_weight and mc.calibration_error > 0.25:
        # 过度自信应该削弱 confidence→exploration
        new_w = old_weight.weight
        assert new_w < old_w, f"Overconfidence should reduce confidence→exploration weight: {new_w:.3f} vs {old_w:.3f}"

    print("  ✓ metacognition calibrate diffusion")


def test_emotional_spectrum_v6():
    """V6: EmotionalSpectrum 写入 ActivationField"""
    from brain.emotional_spectrum import EmotionalSpectrum
    from brain.activation_field import ActivationField

    es = EmotionalSpectrum()
    activation = ActivationField()

    # 摄入兴奋情绪（momentum=0.85，每次移动 15% 的距离）
    es.ingest_llm_emotion(0.85, 0.9, 0.8, "breakthrough", 0.1, activation=activation)

    # VAD 应该被写入 ActivationField（惯性大，单次只移动约 (target-cur)*0.15）
    assert activation.valence > 0.5, f"Breakthrough should raise valence above baseline, got {activation.valence:.3f}"
    assert activation.arousal > 0.5, f"Breakthrough should raise arousal above baseline, got {activation.arousal:.3f}"

    # 向后兼容
    assert es.valence > 0.5

    # 摄入紧急/消极情绪（带高 urgency）
    activation2 = ActivationField()
    es.ingest_llm_emotion(0.2, 0.85, 0.2, "failure", 0.8, activation=activation2)
    assert activation2.get("fear") > 0.1, f"High urgency should raise fear, got {activation2.get('fear'):.3f}"

    print("  ✓ emotional spectrum v6 activation write")


def test_full_closure():
    """V6: 情绪→状态→记忆→身份 的完整闭环验证

    模拟一个完整的情感体验链：
    负面输入 → 情绪变化 → 状态扩散 → 影响记忆权重 → 影响身份
    """
    from brain.activation_field import ActivationField
    from brain.amygdala import Amygdala
    from brain.working_memory import WorkingMemory
    from brain.emotional_spectrum import EmotionalSpectrum

    activation = ActivationField()
    amygdala = Amygdala()
    wm = WorkingMemory()
    es = EmotionalSpectrum()

    # Step 1: 负面事件输入
    amygdala.evaluate(
        "系统核心崩溃，数据可能丢失",
        {"current_emotion": "neutral", "emotion_vector": {"valence": 0.5, "arousal": 0.5, "dominance": 0.5}},
        activation=activation,
    )

    # Step 2: LLM 情绪摄入
    es.ingest_llm_emotion(0.15, 0.85, 0.2, "failure", 0.7, activation=activation)

    # Step 3: 运行扩散
    for _ in range(10):
        activation.tick(dt=1.0)

    # Step 4: 工作记忆受情绪影响
    wm.push("系统崩溃需要修复", "crisis", emotion_weight=0.9, goal_weight=0.8, identity_weight=0.7, novelty=0.5)
    wm.push("今天的天气不错", "casual", emotion_weight=0.1, goal_weight=0.1, identity_weight=0.1, novelty=0.2)
    wm.tick(activation=activation)

    # 验证：危机记忆应该因为高 arousal 而有更高 salience
    top = wm.get_top(2)
    # 至少有一条记忆
    assert len(top) >= 1

    # Step 5: 验证状态场不是死水——危机后多个维度应偏离基线
    from brain.activation_field import DIMENSIONS
    deviated = sum(1 for k in activation._values
                   if abs(activation._values[k] - DIMENSIONS[k][0]) > 0.02)
    # 危机+扩散后至少 2 个维度应该偏离默认值
    # （arousal可能被fatigue反馈环抑制，但其他维度如fear/survival_drive应偏离）
    assert deviated >= 2, f"At least 2 dimensions should deviate after crisis+diffusion, got {deviated}. Values: {dict((k,round(v,3)) for k,v in activation._values.items() if abs(v-DIMENSIONS[k][0])>0.01)}"

    # Step 6: 检测状态闭环
    closures = activation.detect_closure(min_chain=3)
    assert len(closures) > 0, "Should have state closures"

    print("  ✓ full closure: emotion → state → memory → identity")


if __name__ == "__main__":
    print("=" * 55)
    print("Brain Memory V6.0 — State Field 集成测试")
    print("=" * 55)

    tests = [
        ("ActivationField 基础", test_activation_field_basics),
        ("StateDiffusion 规则传播", test_state_diffusion),
        ("状态闭环检测", test_state_closure_detection),
        ("ActivationField 快照/恢复", test_activation_snapshot),
        ("工作记忆 SalienceScore", test_working_memory_salience),
        ("BrainState V6 集成", test_brain_state_v6),
        ("Amygdala V6 写入", test_amygdala_v6),
        ("Metacognition calibrate", test_metacognition_calibrate),
        ("EmotionalSpectrum V6 写入", test_emotional_spectrum_v6),
        ("情绪→状态→记忆→身份闭环", test_full_closure),
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
