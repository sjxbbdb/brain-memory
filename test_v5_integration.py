"""Brain Memory v5.4 — 集成测试。

覆盖所有 v5.0-v5.4 新增模块:
  v5.0: self_model, curiosity, session, intent, agent_bridge
  v5.1: goal_system
  v5.2: metacognition
  v5.3: emotional_spectrum
  v5.4: procedural_memory, time_sense
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_goal_system():
    """v5.1: 目标引擎"""
    from brain.goal_system import GoalSystem
    gs = GoalSystem(max_active=2)

    drives = {"curiosity": 0.8, "coherence": 0.6, "growth": 0.4}
    goals = gs.generate_goals(drives, ["Python", "API"], [], 42, 0)
    assert len(goals) >= 1, f"Expected >=1 goals, got {len(goals)}"
    assert goals[0].drive in drives, f"Unknown drive: {goals[0].drive}"

    # tick and complete
    for _ in range(5):
        gs.tick_goals(100)
    for g in gs.get_active():
        gs.mark_done(g.id)
    assert gs.total_completed > 0
    print("  ✓ goal_system")


def test_metacognition():
    """v5.2: 元认知"""
    from brain.metacognition import Metacognition
    mc = Metacognition()

    # Simulate confident but wrong
    for _ in range(5):
        mc.feed_intent("call_tool", 0.9, "web_search")
    for _ in range(3):
        mc.feed_outcome(False, 0.9)
    mc.feed_outcome(True, 0.8)
    mc.feed_outcome(True, 0.8)

    assert mc.calibration_error > 0, "Should detect overconfidence"
    assert mc.high_confidence_failures >= 1

    insight = mc.get_insight()
    assert len(insight) > 0

    snap = mc.snapshot()
    assert snap["confidence_calibration"]["calibration_status"] == "overconfident"
    print("  ✓ metacognition")


def test_emotional_spectrum():
    """v5.3: 情感光谱"""
    from brain.emotional_spectrum import EmotionalSpectrum
    es = EmotionalSpectrum()

    es.ingest_llm_emotion(0.7, 0.6, 0.55, "curious", 0.2)
    es.ingest_llm_emotion(0.3, 0.8, 0.3, "frustrated", 0.5)

    for _ in range(30):
        es.tick()

    assert len(es.blends) >= 1, "Should have emotion blends"
    assert 0 <= es.valence <= 1
    expr = es.get_expression()
    assert len(expr) > 5

    # Decision modulation
    mod = es.modulate_intent_confidence("call_tool", 0.7)
    assert 0 < mod <= 1

    snap = es.snapshot()
    assert "vad" in snap
    assert "expression" in snap
    print("  ✓ emotional_spectrum")


def test_procedural_memory():
    """v5.4: 程序记忆"""
    from brain.procedural_memory import ProceduralMemory
    pm = ProceduralMemory()

    # Feed experiences
    for i in range(5):
        pm.record_experience("call_tool", "web_search", True, f"query {i}", "results")
    pm.record_experience("call_tool", "web_search", False, "fail", "error")

    assert len(pm.skills) >= 1, "Should learn at least 1 skill"

    # Match
    skill = pm.match_skill("搜索信息", "查找资料")
    if skill:
        assert skill.confidence > 0

    # Decay
    pm.decay_skills()
    print("  ✓ procedural_memory")


def test_time_sense():
    """v5.4: 时间感"""
    from brain.time_sense import TimeSense
    ts = TimeSense()

    for i in range(100):
        ts.tick(i % 3 == 0, i, i * 2)
    ts.record_event("input", "测试输入")
    ts.record_event("success", "操作成功")

    assert ts.age_minutes > 0
    assert ts.subjective_speed > 0

    narrative = ts.get_temporal_narrative()
    assert len(narrative) > 10

    last = ts.time_since("input")
    assert last is not None

    rhythm = ts.get_rhythm_insight()
    # rhythm may be None if not enough data, that's fine

    snap = ts.snapshot()
    assert "time_of_day" in snap
    assert "temporal_narrative" in snap
    print("  ✓ time_sense")


def test_module_imports():
    """验证所有 v5.x 模块可导入"""
    modules = [
        ("brain.goal_system", "GoalSystem"),
        ("brain.metacognition", "Metacognition"),
        ("brain.emotional_spectrum", "EmotionalSpectrum"),
        ("brain.procedural_memory", "ProceduralMemory"),
        ("brain.time_sense", "TimeSense"),
        ("brain.self_model", "SelfModel"),
        ("brain.curiosity", "CuriosityEngine"),
        ("brain.intent", "Intent"),
        ("brain.session", "SessionManager"),
    ]
    for mod_name, cls_name in modules:
        mod = __import__(mod_name, fromlist=[cls_name])
        cls = getattr(mod, cls_name)
        assert cls is not None, f"Failed to import {cls_name} from {mod_name}"
    print("  ✓ all imports")


def test_brain_stem_init():
    """验证 BrainStem 可以初始化（不需要 LLM/DB）"""
    from brain.brain_stem import BrainStem
    bs = BrainStem()
    assert bs.goal_system is not None
    assert bs.metacognition is not None
    assert bs.emotional_spectrum is not None
    assert bs.procedural_memory is not None
    assert bs.time_sense is not None
    assert bs.thalamus is not None
    assert bs.amygdala is not None
    assert bs.working_memory is not None
    print("  ✓ brain_stem init")


def test_cross_module_integration():
    """v5.2→v5.4 跨模块协作: metacognition + emotional + procedural"""
    from brain.metacognition import Metacognition
    from brain.emotional_spectrum import EmotionalSpectrum
    from brain.procedural_memory import ProceduralMemory

    mc = Metacognition()
    es = EmotionalSpectrum()
    pm = ProceduralMemory()

    # Simulate a full interaction cycle
    # 1. LLM produces intent with emotion
    es.ingest_llm_emotion(0.6, 0.5, 0.6, "curious", 0.2)
    es.tick()

    # 2. Metacognition tracks the intent
    mc.feed_intent("call_tool", 0.7, "web_search")

    # 3. Tool executes — record experience
    pm.record_experience("call_tool", "web_search", True, "search query", "results found")

    # 4. Feed outcome
    mc.feed_outcome(True, 0.7)

    # 5. Update cognitive load
    mc.update_cognitive_load(True, True, 1)

    # Verify coherence
    assert mc.total_intents_tracked == 1
    assert len(pm._experience_buffer) == 1
    assert es.dominant_emotion is not None

    print("  ✓ cross-module integration")


if __name__ == "__main__":
    print("=" * 50)
    print("Brain Memory v5.4 — 集成测试")
    print("=" * 50)

    tests = [
        ("v5.0+ 模块导入", test_module_imports),
        ("v5.0+ BrainStem 初始化", test_brain_stem_init),
        ("v5.1 目标引擎", test_goal_system),
        ("v5.2 元认知", test_metacognition),
        ("v5.3 情感光谱", test_emotional_spectrum),
        ("v5.4 程序记忆", test_procedural_memory),
        ("v5.4 时间感", test_time_sense),
        ("v5.2→v5.4 跨模块协作", test_cross_module_integration),
    ]

    passed = 0
    failed = 0

    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}")

    print()
    print(f"{'='*50}")
    print(f"结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    print(f"{'='*50}")

    sys.exit(1 if failed > 0 else 0)
