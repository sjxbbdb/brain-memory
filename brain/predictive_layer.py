"""Predictive Layer — V9 预测加工引擎。自由能原理的最小实现。

核心洞察:
  salience 不应该由 LLM 打分——应该是预测误差。
  "重要" = "出乎意料" + "后果严重"。

  第一个系统自己感受，第二个可以推理。

架构:
  1. ExpectationBuilder — 基于历史模式生成多维度预测
  2. ErrorComputer — 计算预测与实际之间的多维误差
  3. SurpriseHandler — 误差超过阈值时的级联反应

不依赖 LLM。完全基于统计 + 规则。
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v9.predictive-layer")

# ══════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════


@dataclass
class Expectation:
    """多维预测——系统对"接下来会发生什么"的预期。"""

    # 内容维度
    expected_topics: list[str] = field(default_factory=list)  # 预期的话题关键词
    expected_entities: list[str] = field(default_factory=list)  # 预期的实体

    # 情感维度
    expected_valence: float = 0.5  # 预期的情绪效价
    expected_arousal: float = 0.5  # 预期的唤醒度

    # 时间维度
    expected_input_interval_sec: float = 120.0  # 预期的输入间隔

    # 来源维度
    expected_source: Optional[str] = None  # 预期来自谁

    # 元数据
    generated_at: str = ""
    based_on_ticks: int = 0  # 基于多少历史 tick 生成
    confidence: float = 0.5  # 这个预测本身的置信度

    def to_dict(self) -> dict:
        return {
            "topics": self.expected_topics,
            "entities": self.expected_entities,
            "valence": self.expected_valence,
            "arousal": self.expected_arousal,
            "interval_sec": round(self.expected_input_interval_sec, 1),
            "source": self.expected_source,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class PredictionError:
    """预测误差——系统的"惊讶"。"""

    total_error: float  # 0-1，综合误差
    components: dict[str, float]  # 各维度误差分解

    # 维度级误差
    content_error: float = 0.0  # 话题不匹配
    entity_error: float = 0.0  # 实体不匹配
    emotion_error: float = 0.0  # 情绪不匹配
    timing_error: float = 0.0  # 时机不匹配（太早/太晚）
    source_error: float = 0.0  # 来源不匹配

    # 定性标签
    is_surprising: bool = False  # 误差 > SURPRISE_THRESHOLD
    is_shocking: bool = False  # 误差 > SHOCK_THRESHOLD
    surprise_type: str = "none"  # none / mild / surprising / shocking

    def to_dict(self) -> dict:
        return {
            "total": round(self.total_error, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "is_surprising": self.is_surprising,
            "is_shocking": self.is_shocking,
            "surprise_type": self.surprise_type,
        }


# ══════════════════════════════════════════════
# 阈值配置
# ══════════════════════════════════════════════

SURPRISE_THRESHOLD = 0.35  # 超过此值 → 惊讶
SHOCK_THRESHOLD = 0.65  # 超过此值 → 震惊
SALIENCE_FROM_SURPRISE_WEIGHT = 0.6  # 惊讶转化为 salience 的权重

# 误差维度权重（总和 = 1.0）
ERROR_WEIGHTS = {
    "content": 0.25,  # 内容不匹配
    "entity": 0.20,  # 实体不匹配
    "emotion": 0.25,  # 情绪不匹配（最重要的惊讶来源）
    "timing": 0.15,  # 时机不匹配
    "source": 0.15,  # 来源不匹配
}


# ══════════════════════════════════════════════
# ExpectationBuilder
# ══════════════════════════════════════════════


class ExpectationBuilder:
    """基于历史模式构建预测。纯统计，不调 LLM。"""

    def __init__(self, history_size: int = 20):
        self.history: deque[dict] = deque(maxlen=history_size)
        self.topic_transitions: dict[str, dict[str, int]] = {}  # 话题→下一个话题的计数
        self.source_patterns: dict[str, list[float]] = {}  # 来源→间隔列表
        self.emotion_trajectory: deque[tuple[float, float]] = deque(
            maxlen=10
        )  # (valence, arousal) 轨迹
        self.total_observations: int = 0

    def observe(self, input_data: dict, emotion_vector: dict, tick: int):
        """记录一次观察——训练预测模型。"""
        text = input_data.get("text", "")[:200]
        source = input_data.get("source", "unknown")

        entry = {
            "text_snippet": text,
            "source": source,
            "valence": emotion_vector.get("valence", 0.5),
            "arousal": emotion_vector.get("arousal", 0.5),
            "tick": tick,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.history.append(entry)
        self.emotion_trajectory.append((entry["valence"], entry["arousal"]))

        # 更新来源间隔模式
        if source not in self.source_patterns:
            self.source_patterns[source] = []
        if len(self.history) >= 2:
            prev = self.history[-2]
            if prev["source"] == source:
                interval = tick - prev["tick"]
                self.source_patterns[source].append(interval)

        # 更新话题转移矩阵（简单版：用文本首词做话题）
        if len(self.history) >= 2:
            prev_topic = self._extract_topic(self.history[-2]["text_snippet"])
            curr_topic = self._extract_topic(text)
            if prev_topic not in self.topic_transitions:
                self.topic_transitions[prev_topic] = {}
            self.topic_transitions[prev_topic][curr_topic] = (
                self.topic_transitions[prev_topic].get(curr_topic, 0) + 1
            )

        self.total_observations += 1

    def build_expectation(
        self,
        current_tick: int,
        entities_from_wm: list[str],
        time_sense: Any = None,
    ) -> Expectation:
        """构建对下一个 tick 的预测。"""
        exp = Expectation(generated_at=datetime.now(timezone.utc).isoformat())

        # ── 话题预测 ──
        if self.history:
            last_topic = self._extract_topic(self.history[-1]["text_snippet"])
            transitions = self.topic_transitions.get(last_topic, {})
            if transitions:
                total = sum(transitions.values())
                # 选概率最高的下一个话题
                best_topic = max(transitions, key=transitions.get)
                prob = transitions[best_topic] / total
                exp.expected_topics = [best_topic]
                exp.confidence = min(exp.confidence, prob)

        # ── 实体预测（来自工作记忆中的实体）──
        if entities_from_wm:
            exp.expected_entities = entities_from_wm[:5]

        # ── 情绪预测（线性外推最近的情绪轨迹）──
        if len(self.emotion_trajectory) >= 3:
            vs = [t[0] for t in list(self.emotion_trajectory)[-5:]]
            ars = [t[1] for t in list(self.emotion_trajectory)[-5:]]

            # 简单线性回归：t 期的变化率
            if len(vs) >= 2:
                v_delta = (vs[-1] - vs[-2]) * 2  # 外推一步
                a_delta = (ars[-1] - ars[-2]) * 2
                exp.expected_valence = max(0.0, min(1.0, vs[-1] + v_delta))
                exp.expected_arousal = max(0.0, min(1.0, ars[-1] + a_delta))
            else:
                exp.expected_valence = vs[-1]
                exp.expected_arousal = ars[-1]

        # ── 来源预测 ──
        if self.history:
            # 预测和最近一次相同的来源（短期连续性）
            recent_sources = [h["source"] for h in list(self.history)[-5:]]
            if recent_sources:
                # 找最常见的来源
                from collections import Counter

                source_counts = Counter(recent_sources)
                exp.expected_source = source_counts.most_common(1)[0][0]

        # ── 时间预测（基于来源的历史间隔中位数）──
        if exp.expected_source and exp.expected_source in self.source_patterns:
            intervals = self.source_patterns[exp.expected_source]
            if intervals:
                intervals_sorted = sorted(intervals)
                exp.expected_input_interval_sec = (
                    intervals_sorted[len(intervals_sorted) // 2] * 2
                )  # tick → 秒

        exp.based_on_ticks = len(self.history)
        return exp

    def _extract_topic(self, text: str) -> str:
        """简单的无监督话题提取——取前两个有意义的中文词。"""
        # 简化版：用关键词替代 NLP  pipeline
        keywords = [
            "记忆", "目标", "情绪", "反思", "探索", "身份", "错误",
            "成功", "学习", "对话", "搜索", "代码", "配置", "系统",
            "identity", "goal", "memory", "emotion", "error", "success",
        ]
        found = [kw for kw in keywords if kw in text.lower()]
        return ":".join(found[:2]) if found else "general"


# ══════════════════════════════════════════════
# ErrorComputer
# ══════════════════════════════════════════════


class ErrorComputer:
    """计算预测与实际之间的多维误差。"""

    def compute(
        self,
        expectation: Expectation,
        actual_input: dict,
        actual_emotion: dict,
        ticks_since_last_input: int,
    ) -> PredictionError:
        """计算综合预测误差。"""
        components = {}

        # ── 内容误差 ──
        actual_text = actual_input.get("text", "").lower()
        if expectation.expected_topics:
            topic_hits = sum(
                1 for t in expectation.expected_topics if t.lower() in actual_text
            )
            components["content"] = 1.0 - (topic_hits / len(expectation.expected_topics))
        else:
            components["content"] = 0.5  # 没有预测 → 中等不确定性

        # ── 实体误差 ──
        if expectation.expected_entities:
            entity_hits = sum(
                1
                for e in expectation.expected_entities
                if e.lower() in actual_text
            )
            components["entity"] = 1.0 - (entity_hits / len(expectation.expected_entities))
        else:
            components["entity"] = 0.0

        # ── 情绪误差（欧氏距离，归一化）──
        actual_v = actual_emotion.get("valence", 0.5)
        actual_a = actual_emotion.get("arousal", 0.5)
        emotion_dist = math.sqrt(
            (expectation.expected_valence - actual_v) ** 2
            + (expectation.expected_arousal - actual_a) ** 2
        )
        components["emotion"] = min(1.0, emotion_dist)  # 最大距离 sqrt(2) ≈ 1.414

        # ── 时机误差 ──
        # 如果输入比预期早或晚很多 → 误差
        expected_ticks = expectation.expected_input_interval_sec / 2.0  # 转成 tick 数
        if expected_ticks > 0:
            timing_ratio = ticks_since_last_input / max(expected_ticks, 1)
            # timing_ratio ≈ 1.0 表示刚好符合预期
            # timing_ratio >> 1.0 表示等太久了
            # timing_ratio << 1.0 表示来得太早了
            components["timing"] = min(1.0, abs(math.log2(max(timing_ratio, 0.125))))
        else:
            components["timing"] = 0.0

        # ── 来源误差 ──
        actual_source = actual_input.get("source", "unknown")
        if expectation.expected_source:
            components["source"] = (
                0.0 if actual_source == expectation.expected_source else 0.8
            )
        else:
            components["source"] = 0.0

        # ── 加权综合 ──
        total = sum(
            components.get(k, 0.0) * ERROR_WEIGHTS.get(k, 0.0) for k in ERROR_WEIGHTS
        )

        error = PredictionError(
            total_error=total,
            components=components,
            content_error=components.get("content", 0.0),
            entity_error=components.get("entity", 0.0),
            emotion_error=components.get("emotion", 0.0),
            timing_error=components.get("timing", 0.0),
            source_error=components.get("source", 0.0),
        )

        # ── 定性标签 ──
        if total >= SHOCK_THRESHOLD:
            error.is_shocking = True
            error.is_surprising = True
            error.surprise_type = "shocking"
        elif total >= SURPRISE_THRESHOLD:
            error.is_surprising = True
            error.surprise_type = "surprising"
        elif total >= 0.15:
            error.surprise_type = "mild"

        return error


# ══════════════════════════════════════════════
# SurpriseHandler — 惊讶的级联反应
# ══════════════════════════════════════════════


class SurpriseHandler:
    """处理预测误差——将"惊讶"转化为认知和行为变化。"""

    def __init__(self):
        self.total_surprises: int = 0
        self.total_shocks: int = 0
        self.recent_surprises: deque[dict] = deque(maxlen=20)

    def handle(
        self,
        error: PredictionError,
        working_memory: Any,
        curiosity: Any,
        hippocampus: Any,
        exploration_queue: Any,
    ) -> dict:
        """处理一次惊讶事件。返回副作用描述。"""
        result = {"actions": [], "salience_boost": 0.0}

        if not error.is_surprising:
            return result

        self.total_surprises += 1
        if error.is_shocking:
            self.total_shocks += 1

        # ── 1. 将惊讶转化为 salience ──
        #     这是最关键的一步：salience 不再来自 LLM，来自系统自身
        salience_boost = error.total_error * SALIENCE_FROM_SURPRISE_WEIGHT
        result["salience_boost"] = round(salience_boost, 3)

        # ── 2. 推送惊讶到工作记忆 ──
        reason_parts = []
        if error.emotion_error > 0.3:
            reason_parts.append("情绪出乎意料")
        if error.content_error > 0.3:
            reason_parts.append("话题不对")
        if error.source_error > 0.3:
            reason_parts.append("没想到是这个人")
        if error.timing_error > 0.3:
            reason_parts.append("时机不对")

        reason = "、".join(reason_parts) if reason_parts else "综合预测失败"

        surprise_message = f"[惊讶] {reason}"
        if error.is_shocking:
            surprise_message = f"[震惊] {reason} — 我的世界模型可能有问题"

        working_memory.push(
            content=surprise_message[:200],
            source="predictive_layer",
            base_salience=salience_boost + 0.3,
        )
        result["actions"].append("wm_push")

        # ── 3. 触发好奇 — "为什么我的预测错了？" ──
        if error.is_surprising and curiosity:
            # 根据主导误差维度生成问题
            max_dim = max(error.components, key=error.components.get)
            questions = {
                "emotion": "为什么我对情绪的判断出错了？",
                "content": "为什么讨论的话题变了？",
                "source": "为什么是这个人在说话？",
                "timing": "为什么事情发生得比我预期的早/晚？",
                "entity": "为什么会提到我不认识的实体？",
            }
            question = questions.get(max_dim, "为什么我的预测出错了？")
            curiosity.open_questions.append(
                {"question": question, "source": "prediction_error", "created": datetime.now(timezone.utc).isoformat()}
            )
            result["actions"].append(f"curiosity: {question[:60]}")

        # ── 4. 如果是震惊级别，触发探索任务 ──
        if error.is_shocking and exploration_queue:
            exploration_queue.add_task(
                source="prediction_error",
                question=f"我的世界模型严重出错: {reason}",
                priority=0.75,
            )
            result["actions"].append("exploration_task_created")

        # ── 5. 标记与此次惊讶相关的旧记忆（需要 hippocampus 支持）──
        #    将来实现: 找到预测基于的那些记忆，标记它们的预测价值下降
        #    "上次这条记忆引导了错误预测 → 降低它的预测权重"

        # ── 6. 记录 ──
        self.recent_surprises.append(
            {
                "error": error.total_error,
                "surprise_type": error.surprise_type,
                "components": dict(error.components),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        logger.info(
            "surprise-handler: %s (error=%.4f, actions=%s)",
            error.surprise_type,
            error.total_error,
            result["actions"],
        )

        return result

    def snapshot(self) -> dict:
        return {
            "total_surprises": self.total_surprises,
            "total_shocks": self.total_shocks,
            "recent": list(self.recent_surprises)[-5:],
        }


# ══════════════════════════════════════════════
# PredictiveLayer — 顶层封装
# ══════════════════════════════════════════════


class PredictiveLayer:
    """预测加工引擎的顶层入口。

    在 brain_stem._tick() 中的调用方式:
        # 输入到达前: 生成预测
        expectation = brain_stem.predictive_layer.build_expectation(state, ...)

        # 输入到达后: 计算误差
        error = brain_stem.predictive_layer.compute_error(expectation, input, emotion)
        if error.is_surprising:
            # salience 不再由 LLM 打分，而是:
            salience = error.total_error * SALIENCE_FROM_SURPRISE_WEIGHT
            amygdala_out['salience'] = max(amygdala_out.get('salience',0), salience)

            # 处理惊讶
            brain_stem.predictive_layer.handle_surprise(error, ...)
    """

    def __init__(self):
        self.builder = ExpectationBuilder(history_size=30)
        self.computer = ErrorComputer()
        self.handler = SurpriseHandler()
        self.last_expectation: Optional[Expectation] = None
        self.last_error: Optional[PredictionError] = None
        self.enabled: bool = True

    def build_expectation(
        self,
        current_tick: int,
        entities_from_wm: list[str],
        time_sense: Any = None,
    ) -> Expectation:
        """生成对下一个输入的预测。"""
        exp = self.builder.build_expectation(
            current_tick=current_tick,
            entities_from_wm=entities_from_wm,
            time_sense=time_sense,
        )
        self.last_expectation = exp
        return exp

    def observe_and_compute(
        self,
        expectation: Expectation,
        input_data: dict,
        emotion_vector: dict,
        ticks_since_input: int,
    ) -> PredictionError:
        """观察实际输入 + 计算预测误差（合并调用）。"""
        # 1. 训练预测模型
        self.builder.observe(input_data, emotion_vector, ticks_since_input)

        # 2. 计算误差
        error = self.computer.compute(
            expectation=expectation,
            actual_input=input_data,
            actual_emotion=emotion_vector,
            ticks_since_last_input=ticks_since_input,
        )
        self.last_error = error
        return error

    def handle_surprise(
        self,
        error: PredictionError,
        working_memory: Any,
        curiosity: Any,
        hippocampus: Any,
        exploration_queue: Any,
    ) -> dict:
        """处理惊讶。"""
        return self.handler.handle(
            error=error,
            working_memory=working_memory,
            curiosity=curiosity,
            hippocampus=hippocampus,
            exploration_queue=exploration_queue,
        )

    @property
    def salience_from_surprise(self) -> float:
        """从上次预测误差中提取 salience。"""
        if self.last_error and self.last_error.is_surprising:
            return self.last_error.total_error * SALIENCE_FROM_SURPRISE_WEIGHT
        return 0.0

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "builder_observations": self.builder.total_observations,
            "last_expectation": self.last_expectation.to_dict()
            if self.last_expectation
            else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "handler": self.handler.snapshot(),
        }
