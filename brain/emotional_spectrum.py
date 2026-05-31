"""Emotional Spectrum — V6 情感光谱。连续情感空间 + ActivationField 写入。

V6 升级: ingest_llm_emotion() 直接写入 ActivationField，
  情感混合(blends)和轨迹追踪保留用于自然语言表达和仪表盘。
  不再维护独立的 VAD 状态——ActivationField 是唯一真相源。
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v5.emotional-spectrum")


# ══════════════════════════════════════════════
# 情感原色 — 基础情感维度
# ══════════════════════════════════════════════

# 在 VAD 空间中的情感锚点（用于 LLM 输出→连续空间映射）
EMOTION_ANCHORS = {
    "curious":     {"valence": 0.65, "arousal": 0.60, "dominance": 0.55},
    "excited":     {"valence": 0.80, "arousal": 0.85, "dominance": 0.70},
    "satisfied":   {"valence": 0.75, "arousal": 0.30, "dominance": 0.65},
    "neutral":     {"valence": 0.50, "arousal": 0.50, "dominance": 0.50},
    "confused":    {"valence": 0.35, "arousal": 0.55, "dominance": 0.35},
    "worried":     {"valence": 0.25, "arousal": 0.65, "dominance": 0.30},
    "frustrated":  {"valence": 0.20, "arousal": 0.80, "dominance": 0.45},
    "disappointed": {"valence": 0.25, "arousal": 0.35, "dominance": 0.35},
    "failure":     {"valence": 0.15, "arousal": 0.70, "dominance": 0.20},
    "breakthrough": {"valence": 0.85, "arousal": 0.90, "dominance": 0.85},
    "hopeful":     {"valence": 0.70, "arousal": 0.55, "dominance": 0.50},
    "cautious":    {"valence": 0.45, "arousal": 0.40, "dominance": 0.40},
    "determined":  {"valence": 0.60, "arousal": 0.70, "dominance": 0.80},
    "melancholic": {"valence": 0.30, "arousal": 0.25, "dominance": 0.30},
    "surprised":   {"valence": 0.55, "arousal": 0.80, "dominance": 0.45},
}


@dataclass
class EmotionBlend:
    """一种情感成分及其强度。"""
    name: str        # e.g. "curious", "worried"
    intensity: float # 0.0-1.0
    valence: float
    arousal: float
    dominance: float

    @property
    def label(self) -> str:
        """人类可读标签。"""
        if self.intensity < 0.15:
            return f"微弱的{self.name}"
        elif self.intensity < 0.35:
            return f"轻微的{self.name}"
        elif self.intensity < 0.60:
            return self.name
        elif self.intensity < 0.80:
            return f"明显的{self.name}"
        else:
            return f"强烈的{self.name}"


@dataclass
class EmotionalState:
    """一个时刻的情感快照。"""
    timestamp: str
    valence: float
    arousal: float
    dominance: float
    dominant_emotion: str
    blends: list[dict]  # [{name, intensity}]
    expression: str     # 自然语言表达


class EmotionalSpectrum:
    """情感光谱引擎——从离散标签到连续情感空间。

    替换杏仁核的"情绪标签"角色，提供:
      1. 连续 VAD 空间 + 情感动量
      2. 混合情感（可以同时好奇+担忧）
      3. 情感轨迹追踪
      4. 情感→决策调制
      5. 自然语言情感表达
    """

    def __init__(self):
        # ── VAD 核心 ──
        self.valence: float = 0.5      # 0=负面 1=正面
        self.arousal: float = 0.5      # 0=平静 1=兴奋
        self.dominance: float = 0.5    # 0=受控 1=掌控

        # ── 情感混合 ──
        self.blends: dict[str, float] = {"neutral": 0.8}  # {emotion_name: intensity}
        self.dominant_emotion: str = "neutral"
        self.secondary_emotion: str | None = None

        # ── 情感动量 ──
        self.momentum: float = 0.85        # 惯性系数（0=无记忆 1=完全不变化）
        self._target_valence: float = 0.5  # LLM 输出的目标 VAD（经过惯性后慢慢到达）
        self._target_arousal: float = 0.5
        self._target_dominance: float = 0.5

        # ── 情感轨迹 ──
        self.trajectory: deque = deque(maxlen=60)  # EmotionalState 快照
        self._snapshot_interval: int = 30          # 每 30 tick 记录一次
        self._ticks_since_snapshot: int = 0

        # ── 基线 ──
        self.baseline_valence: float = 0.5
        self.baseline_arousal: float = 0.5
        self.baseline_dominance: float = 0.5
        self._baseline_decay: float = 0.998         # 基线缓慢回归

        # ── 统计 ──
        self.emotion_shifts: list[dict] = []         # 重大情绪变化记录
        self.total_llm_updates: int = 0

    # ══════════════════════════════════════════════
    # 核心 API — LLM 输出后调用
    # ══════════════════════════════════════════════

    def ingest_llm_emotion(
        self,
        valence: float,
        arousal: float,
        dominance: float,
        label: str = "neutral",
        urgency: float = 0.0,
        activation=None,
    ):
        """摄入 LLM 产出的情绪信号——带动量平滑。

        V6: 如果提供 activation，直接写入 ActivationField 的 VAD 维度。
        不是直接替换当前情绪，而是设定"目标"然后让情绪慢慢漂移过去。
        """
        self.total_llm_updates += 1

        # V6: 写入 ActivationField（带惯性）
        if activation is not None:
            # 从 ActivationField 读取当前值
            cur_v = activation.get("valence")
            cur_a = activation.get("arousal")
            cur_d = activation.get("dominance")

            # 动量平滑
            m = self.momentum
            activation.set("valence", cur_v * m + valence * (1 - m))
            activation.set("arousal", cur_a * m + arousal * (1 - m))
            activation.set("dominance", cur_d * m + dominance * (1 - m))

            # urgency → fear 维度
            if urgency > 0.3:
                cur_fear = activation.get("fear")
                activation.set("fear", cur_fear * 0.8 + urgency * 0.2)

            # 同步到自身 VAD（向后兼容）
            self.valence = activation.get("valence")
            self.arousal = activation.get("arousal")
            self.dominance = activation.get("dominance")

        # 设定目标 VAD（向后兼容：无 activation 时保持旧行为）
        self._target_valence = max(0.0, min(1.0, valence))
        self._target_arousal = max(0.0, min(1.0, arousal))
        self._target_dominance = max(0.0, min(1.0, dominance))

        # 更新情感混合（保留——用于自然语言表达）
        self._update_blends(label, urgency)

    def tick(self):
        """每个 tick 调用——情感漂移一步。"""
        # ── 动量漂移：当前 VAD 向目标 VAD 缓慢靠近 ──
        m = self.momentum
        self.valence = self.valence * m + self._target_valence * (1 - m)
        self.arousal = self.arousal * m + self._target_arousal * (1 - m)
        self.dominance = self.dominance * m + self._target_dominance * (1 - m)

        # ── 无外部输入时向基线回归 ──
        if self.total_llm_updates == 0 or self._target_valence == 0.5:
            bd = self._baseline_decay
            self.valence = self.valence * bd + self.baseline_valence * (1 - bd)
            self.arousal = self.arousal * bd + self.baseline_arousal * (1 - bd)
            self.dominance = self.dominance * bd + self.baseline_dominance * (1 - bd)

        # ── 混合情感衰减 ──
        self._decay_blends()

        # ── 轨迹快照 ──
        self._ticks_since_snapshot += 1
        if self._ticks_since_snapshot >= self._snapshot_interval:
            self._record_trajectory()
            self._ticks_since_snapshot = 0

    # ══════════════════════════════════════════════
    # 情感→决策调制
    # ══════════════════════════════════════════════

    def modulate_intent_confidence(self, intent_type: str, base_confidence: float) -> float:
        """情感调制 intent 置信度。

        兴奋 → 更敢 call_tool
        担忧 → 更倾向 think
        掌控感强 → 更敢 respond
        """
        factor = 1.0

        if intent_type == "call_tool":
            # 高 arousal 增加行动倾向
            factor += (self.arousal - 0.5) * 0.15
            # 低 dominance 降低行动倾向
            factor += (self.dominance - 0.5) * 0.10
        elif intent_type == "respond":
            # 高 valence 更愿意回应
            factor += (self.valence - 0.5) * 0.12
            # 高 dominance 更自信回应
            factor += (self.dominance - 0.5) * 0.08
        elif intent_type == "think":
            # 高 arousal + 低 valence = 需要思考
            if self.arousal > 0.6 and self.valence < 0.4:
                factor += 0.10
            # 低 arousal = 倾向内省
            factor += (0.5 - self.arousal) * 0.08

        return max(0.1, min(1.0, base_confidence * factor))

    def get_decision_bias(self) -> dict:
        """返回情感对决策的偏向建议。

        用于 brain_stem 在生成 intent 时参考。
        """
        bias = {}

        # 高 arousal → 倾向行动
        if self.arousal > 0.7:
            bias["prefer_action"] = True
            bias["prefer_tool"] = "web_search"
        elif self.arousal < 0.3:
            bias["prefer_action"] = False
            bias["prefer_introspection"] = True

        # 低 valence → 倾向寻求信息/帮助
        if self.valence < 0.3:
            bias["emotional_state"] = "seeking_support"
        elif self.valence > 0.75:
            bias["emotional_state"] = "confident_sharing"

        # 低 dominance → 倾向提问而非断言
        if self.dominance < 0.3:
            bias["communication_style"] = "tentative"
        elif self.dominance > 0.7:
            bias["communication_style"] = "assertive"

        return bias

    # ══════════════════════════════════════════════
    # 情感表达
    # ══════════════════════════════════════════════

    def get_expression(self) -> str:
        """生成自然语言情感表达。"""
        blends_sorted = sorted(self.blends.items(), key=lambda x: x[1], reverse=True)
        if not blends_sorted:
            return "我感觉平静"

        primary = blends_sorted[0]
        secondary = blends_sorted[1] if len(blends_sorted) > 1 else None

        # 强度副词
        def intensity_word(i: float) -> str:
            if i > 0.8: return "非常"
            if i > 0.6: return "很"
            if i > 0.4: return "有些"
            if i > 0.2: return "稍微"
            return "一点点"

        expr = f"我感觉{intensity_word(primary[1])}{primary[0]}"
        if secondary and secondary[1] > 0.25:
            expr += f"，同时也{intensity_word(secondary[1])}{secondary[0]}"

        # 加上 arousal 反馈
        if self.arousal > 0.75:
            expr += "，内心很活跃"
        elif self.arousal < 0.25:
            expr += "，内心很平静"

        return expr

    def get_current_label(self) -> str:
        """获取当前主导情绪标签（向后兼容）。"""
        return self.dominant_emotion

    # ══════════════════════════════════════════════
    # 内部
    # ══════════════════════════════════════════════

    def _update_blends(self, llm_label: str, urgency: float):
        """根据 LLM 标签和 urgency 更新情感混合。"""
        # LLM 标签对应的情感权重增加
        if llm_label in EMOTION_ANCHORS:
            increment = 0.15 + urgency * 0.1
            for name in self.blends:
                self.blends[name] *= 0.85  # 其他情感衰减
            self.blends[llm_label] = min(1.0, self.blends.get(llm_label, 0.0) + increment)

        # 更新主导情感
        if self.blends:
            self.dominant_emotion = max(self.blends, key=self.blends.get)
            sorted_blends = sorted(self.blends.items(), key=lambda x: x[1], reverse=True)
            self.secondary_emotion = sorted_blends[1][0] if len(sorted_blends) > 1 and sorted_blends[1][1] > 0.2 else None

        # 修剪：清除强度过低的情感
        self.blends = {k: v for k, v in self.blends.items() if v > 0.05}
        if not self.blends:
            self.blends = {"neutral": 0.3}

    def _decay_blends(self):
        """所有情感强度缓慢衰减。"""
        for name in list(self.blends.keys()):
            self.blends[name] *= 0.995
            if self.blends[name] < 0.03:
                del self.blends[name]
        if not self.blends:
            self.blends["neutral"] = 0.3

    def _record_trajectory(self):
        """记录情感轨迹快照。"""
        blends_sorted = sorted(self.blends.items(), key=lambda x: x[1], reverse=True)
        state = EmotionalState(
            timestamp=datetime.now(timezone.utc).isoformat(),
            valence=round(self.valence, 3),
            arousal=round(self.arousal, 3),
            dominance=round(self.dominance, 3),
            dominant_emotion=self.dominant_emotion,
            blends=[{"name": n, "intensity": round(i, 2)} for n, i in blends_sorted[:3]],
            expression=self.get_expression(),
        )
        self.trajectory.append(state)

        # 检测重大情绪变化
        if len(self.trajectory) >= 2:
            prev = self.trajectory[-2]
            v_shift = abs(self.valence - prev.valence)
            if v_shift > 0.2:
                self.emotion_shifts.append({
                    "timestamp": state.timestamp,
                    "from": prev.dominant_emotion,
                    "to": self.dominant_emotion,
                    "valence_delta": round(self.valence - prev.valence, 2),
                    "trigger": "emotional_shift",
                })
                if len(self.emotion_shifts) > 30:
                    self.emotion_shifts = self.emotion_shifts[-30:]

    # ══════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════

    def get_vad(self) -> dict:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "dominance": round(self.dominance, 3),
        }

    def get_blends(self) -> list[dict]:
        return sorted(
            [{"name": n, "intensity": round(i, 2), "label": EmotionBlend(n, i, 0, 0, 0).label}
             for n, i in self.blends.items()],
            key=lambda x: x["intensity"], reverse=True,
        )

    def get_trajectory_summary(self) -> str:
        """情感轨迹的自然语言摘要。"""
        if len(self.trajectory) < 2:
            return "情感状态尚未形成轨迹"

        recent = list(self.trajectory)[-5:]
        first, last = recent[0], recent[-1]

        v_trend = "上升" if last.valence > first.valence + 0.05 else (
            "下降" if last.valence < first.valence - 0.05 else "稳定"
        )
        a_trend = "增加" if last.arousal > first.arousal + 0.05 else (
            "减少" if last.arousal < first.arousal - 0.05 else "不变"
        )

        return f"最近情绪 valence {v_trend}，arousal {a_trend}。当前主导: {self.dominant_emotion}。"

    # ══════════════════════════════════════════════
    # 快照
    # ══════════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "vad": self.get_vad(),
            "dominant_emotion": self.dominant_emotion,
            "secondary_emotion": self.secondary_emotion,
            "blends": self.get_blends()[:5],
            "expression": self.get_expression(),
            "momentum": self.momentum,
            "trajectory_summary": self.get_trajectory_summary(),
            "recent_shifts": [
                {"from": s["from"], "to": s["to"], "delta": s["valence_delta"]}
                for s in self.emotion_shifts[-5:]
            ],
            "decision_bias": self.get_decision_bias(),
            "baseline": {
                "valence": round(self.baseline_valence, 2),
                "arousal": round(self.baseline_arousal, 2),
                "dominance": round(self.baseline_dominance, 2),
            },
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "EmotionalSpectrum":
        es = cls()
        if not data:
            return es
        vad = data.get("vad", {})
        es.valence = vad.get("valence", 0.5)
        es.arousal = vad.get("arousal", 0.5)
        es.dominance = vad.get("dominance", 0.5)
        es.dominant_emotion = data.get("dominant_emotion", "neutral")
        es.secondary_emotion = data.get("secondary_emotion")
        blends = data.get("blends", [])
        es.blends = {b["name"]: b["intensity"] for b in blends}
        if not es.blends:
            es.blends = {"neutral": 0.5}
        es.momentum = data.get("momentum", 0.85)
        baseline = data.get("baseline", {})
        es.baseline_valence = baseline.get("valence", 0.5)
        es.baseline_arousal = baseline.get("arousal", 0.5)
        es.baseline_dominance = baseline.get("dominance", 0.5)
        return es
