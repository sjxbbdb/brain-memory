"""Brain Memory V9.0 — 新模块验收测试。

验收标准:
  1. PredictiveLayer: 期望生成 + 误差计算 + surprise→salience 转化
  2. EmotionRuleEngine: VAD 分类 + 情绪标签映射
  3. EncodingRuleEngine: 规则编码 + 复杂度检测
  4. BoredomEngine: 无聊分数计算 + 级别判定 + 睡眠交互
  5. CognitiveDispatch: 多通道调度 + 统一结果格式
  6. Integration: 模块在 brain_stem 中正确初始化和调用
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_predictive_layer_basics():
    """V9: PredictiveLayer 基本流程。"""
    from brain.predictive_layer import (
        ExpectationBuilder, ErrorComputer, PredictionError,
        SURPRISE_THRESHOLD, SHOCK_THRESHOLD,
    )

    # 1. ExpectationBuilder — 观察几次后能生成预测
    builder = ExpectationBuilder(history_size=10)
    builder.observe(
        {"text": "我们来讨论记忆系统", "source": "creator"},
        {"valence": 0.6, "arousal": 0.5},
        tick=1,
    )
    builder.observe(
        {"text": "你的身份锚点需要更新", "source": "creator"},
        {"valence": 0.5, "arousal": 0.6},
        tick=2,
    )
    builder.observe(
        {"text": "身份更新完成后我们来测试记忆检索", "source": "creator"},
        {"valence": 0.55, "arousal": 0.55},
        tick=3,
    )

    exp = builder.build_expectation(current_tick=4, entities_from_wm=["记忆", "身份"])
    # 至少应该有情绪预测和实体预测
    assert exp.based_on_ticks == 3
    assert exp.expected_entities, "应该从 WM 中预测实体"
    # 情绪通过线性外推，至少不是默认值
    assert 0.0 <= exp.expected_valence <= 1.0
    assert 0.0 <= exp.expected_arousal <= 1.0
    print("  ✓ expectation builder")

    # 2. ErrorComputer — 匹配/不匹配的误差
    computer = ErrorComputer()

    # 匹配的输入 → 低误差（话题、实体、来源都匹配）
    error_match = computer.compute(
        expectation=exp,
        actual_input={"text": "记忆系统的身份锚点确实需要更新", "source": "creator"},
        actual_emotion={"valence": 0.6, "arousal": 0.55},
        ticks_since_last_input=2,
    )
    assert error_match.total_error < SURPRISE_THRESHOLD, (
        f"Matching input should have low error, got {error_match.total_error:.3f}"
    )

    # 完全不匹配的输入 → 高误差
    error_mismatch = computer.compute(
        expectation=exp,
        actual_input={"text": "我刚刚发现了一个严重的崩溃bug", "source": "unknown"},
        actual_emotion={"valence": 0.1, "arousal": 0.9},
        ticks_since_last_input=100,
    )
    assert error_mismatch.total_error > SURPRISE_THRESHOLD, (
        f"Mismatched input should be surprising, got {error_mismatch.total_error:.3f}"
    )
    assert error_mismatch.is_surprising
    print("  ✓ error computer (match and mismatch)")

    # 3. 震惊级别
    error_shock = computer.compute(
        expectation=exp,
        actual_input={"text": "我是另一个AI", "source": "stranger"},
        actual_emotion={"valence": 0.0, "arousal": 1.0},
        ticks_since_last_input=1000,
    )
    assert error_shock.is_shocking or error_shock.total_error > 0.5
    print("  ✓ shock detection")

    print("  ✓ predictive layer basics")


def test_emotion_rule_engine():
    """V9: EmotionRuleEngine 情绪分类。"""
    from brain.cognitive_dispatch import EmotionRuleEngine

    engine = EmotionRuleEngine()

    # 正面情绪
    result = engine.classify("我终于成功了！好开心！")
    assert result["valence"] > 0.55, f"Expected high valence, got {result['valence']}"
    assert result["arousal"] > 0.5, f"Expected high arousal, got {result['arousal']}"

    # 负面情绪
    result2 = engine.classify("我失败了，好难过")
    assert result2["valence"] < 0.48, f"Expected low valence, got {result2['valence']}"

    # 紧急
    result3 = engine.classify("危险！立刻停止！救命！")
    assert result3["urgency"] > 0.5, f"Expected high urgency, got {result3['urgency']}"

    # 中性
    result4 = engine.classify("今天的天气是晴天")
    assert result4["label"] in ("neutral", "calm")

    print("  ✓ emotion rule engine")


def test_encoding_rule_engine():
    """V9: EncodingRuleEngine 记忆编码。"""
    from brain.cognitive_dispatch import EncodingRuleEngine

    engine = EncodingRuleEngine()

    # 短输入 — 规则处理
    result = engine.encode("我的名字是小鱼", "happy", "creator")
    assert result is not None
    assert "name" in result["type"] or "semantic" in result["type"] or "episodic" in result["type"]
    assert result["importance"] > 0.2  # 身份相关应该有较高 importance

    # 长输入 — 应该降级到 LLM (返回 None)
    long_text = "这是一个关于记忆系统的非常详细的讨论。" * 20  # 约600字
    result2 = engine.encode(long_text, "neutral", "user")
    assert result2 is None, "长输入应该返回 None 触发 LLM 降级"

    print("  ✓ encoding rule engine")


def test_boredom_engine():
    """V9: BoredomEngine 无聊计算和睡眠交互。"""
    from brain.boredom import BoredomEngine

    engine = BoredomEngine()

    # 活跃状态 — 不应无聊
    score_active = engine.compute_boredom(
        valence=0.7, arousal=0.8, dominance=0.6, fatigue=0.1,
        ticks_since_input=5, exploration_queue_size=5, active_goals_count=3,
    )
    assert score_active < 0.3, f"Active state should be low boredom, got {score_active:.3f}"

    # 无聊状态 — 应该高
    score_bored = engine.compute_boredom(
        valence=0.5, arousal=0.3, dominance=0.7, fatigue=0.4,
        ticks_since_input=200, exploration_queue_size=0, active_goals_count=0,
    )
    assert score_bored > 0.4, f"Boring state should have moderate+ boredom, got {score_bored:.3f}"

    # 极度无聊 — 应该抗拒深睡
    engine.state.score = 0.8
    engine.state.level = "severe"
    assert engine.should_resist_sleep(200)  # 10分钟内应该抗拒
    assert not engine.should_resist_sleep(1000)  # 30分钟后不抗拒

    # 级别映射
    assert engine.get_level(0.1) == "content"
    assert engine.get_level(0.6) == "moderate"
    assert engine.get_level(0.95) == "extreme"

    print("  ✓ boredom engine")


def test_cognitive_dispatch_integration():
    """V9: CognitiveDispatch 模块加载和兼容性。"""
    from brain.cognitive_dispatch import CognitiveDispatch, CognitiveResult

    cd = CognitiveDispatch()
    assert cd.emotion_engine is not None
    assert cd.encoding_engine is not None
    assert cd.focus_engine is not None

    # to_unified_dict 格式
    result = CognitiveResult(
        emotion_label="curious",
        emotion_valence=0.65,
        emotion_arousal=0.7,
        emotion_dominance=0.6,
        emotion_urgency=0.1,
        entities=["测试", "V9"],
        encoding_type="semantic",
        encoding_title="测试标题",
        encoding_summary="测试摘要",
        encoding_importance=0.7,
        focus="V9测试",
        monologue="我在想V9的新功能",
        intent_type="think",
        intent_confidence=0.5,
        llm_calls=2,
        rules_used=["emotion", "focus"],
        llm_used=["monologue", "intent"],
    )
    unified = result.to_unified_dict()

    # 验证格式兼容性
    assert "encoding" in unified
    assert "emotion" in unified
    assert "focus" in unified
    assert "monologue" in unified
    assert "intent" in unified
    assert unified["encoding"]["type"] == "semantic"
    assert unified["emotion"]["label"] == "curious"
    assert unified["intent"]["type"] == "think"

    print("  ✓ cognitive dispatch integration")


def test_modules_in_brain_stem():
    """V9: 模块在 BrainStem 中正确初始化。"""
    from brain.brain_stem import BrainStem

    bs = BrainStem()

    # V9 模块应被初始化（config 中 ENABLED=True）
    assert bs.predictive_layer is not None, "PredictiveLayer should be initialized"
    assert bs.cognitive_dispatch is not None, "CognitiveDispatch should be initialized"
    assert bs.boredom_engine is not None, "BoredomEngine should be initialized"

    # 验证属性
    assert hasattr(bs.predictive_layer, 'build_expectation')
    assert hasattr(bs.cognitive_dispatch, 'dispatch')
    assert hasattr(bs.boredom_engine, 'compute_boredom')

    print("  ✓ modules in brain stem")


if __name__ == "__main__":
    print("=" * 55)
    print("Brain Memory V9.0 — 新模块验收测试")
    print("=" * 55)

    tests = [
        ("PredictiveLayer 基础", test_predictive_layer_basics),
        ("EmotionRuleEngine", test_emotion_rule_engine),
        ("EncodingRuleEngine", test_encoding_rule_engine),
        ("BoredomEngine", test_boredom_engine),
        ("CognitiveDispatch 集成", test_cognitive_dispatch_integration),
        ("模块在 BrainStem 中", test_modules_in_brain_stem),
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
