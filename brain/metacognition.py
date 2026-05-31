"""Metacognition — V6 元认知。大脑对自己思考的思考 + 扩散权重校准。

V6 升级: 元认知不仅监控自身，还校准 StateDiffusionEngine 的规则权重。
  - cognitive_load、uncertainty、confidence 写入 ActivationField
  - 基于校准误差调整扩散规则权重（过度自信→削弱 confidence→exploration 的正向权重）
  - 这是 V6 状态闭环的"学习"机制

设计原则:
  - 轻量：主要是规则引擎+滑动窗口统计，不新增 LLM 调用
  - 信号性：产生"认知告警"而非指令
  - 累积性：偏见不是一次事件，是长期模式
  - 诚实：元认知可以承认"我不知道"
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v6.metacognition")


# ══════════════════════════════════════════════
# 认知偏见模式定义
# ══════════════════════════════════════════════

BIAS_PATTERNS = {
    "overconfidence": {
        "label": "过度自信",
        "detect": "confidence > 0.85 且后续被标记为失败",
        "severity": "high",
    },
    "repetition_loop": {
        "label": "重复循环",
        "detect": "连续 3+ 次产出相同意图但无进展",
        "severity": "medium",
    },
    "recency_trap": {
        "label": "近因陷阱",
        "detect": "工作记忆 80% 内容来自最近 1 个 source",
        "severity": "low",
    },
    "avoidance": {
        "label": "回避模式",
        "detect": "某个高权重驱动力从未产生过目标",
        "severity": "medium",
    },
    "novelty_seeking": {
        "label": "猎奇倾向",
        "detect": "curiosity 驱动占比 > 60% 且其他驱动被冷落",
        "severity": "low",
    },
}


@dataclass
class CognitiveAlert:
    """一条元认知告警。"""
    id: str
    pattern: str           # 偏见模式标识
    label: str             # 人类可读标签
    severity: str          # high/medium/low
    evidence: str          # 触发证据
    suggestion: str        # 建议行动
    created_at: str
    acknowledged: bool = False


class Metacognition:
    """元认知引擎——大脑的自我监控系统。

    四个子系统:
      1. 认知负荷追踪 — 我累不累
      2. 置信度校准 — 我有多相信自己，准不准
      3. 决策审计 — 我的决策后来怎么样了
      4. 偏见检测 — 我有什么思维模式问题
    """

    def __init__(self):
        # ── 1. 认知负荷 ──
        self.cognitive_load: float = 0.3        # 0=空闲 1=过载
        self.load_history: deque = deque(maxlen=50)
        self._recent_processing_intensity: float = 0.0
        self._tick_since_last_rest: int = 0

        # ── 2. 置信度追踪 ──
        self.recent_confidences: deque = deque(maxlen=30)    # (confidence, outcome)
        self.mean_confidence: float = 0.5
        self.actual_success_rate: float = 0.5               # 基于反馈的真实成功率
        self.calibration_error: float = 0.0                  # mean_confidence - success_rate
        self.overconfidence_count: int = 0

        # ── 3. 决策审计 ──
        self.decision_log: deque = deque(maxlen=40)          # [{intent_type, tool, confidence, result, tick}]
        self.success_count: int = 0
        self.failure_count: int = 0
        self.high_confidence_failures: int = 0  # confidence>0.8 但失败

        # ── 4. 偏见检测 ──
        self.intent_type_counts: dict[str, int] = {}         # {intent_type: count}
        self.drive_activity: dict[str, int] = {}             # {drive: goal_count}
        self.last_intents: deque = deque(maxlen=10)          # 最近意图（检测循环）
        self.alerts: list[CognitiveAlert] = []
        self._max_alerts = 20

        # ── 5. 盲区追踪 ──
        self.known_blind_spots: list[str] = []               # 大脑承认自己不懂的
        self.suggested_explorations: list[str] = []          # 元认知建议探索的

        # 统计
        self.total_intents_tracked: int = 0
        self.total_alerts_generated: int = 0

    # ══════════════════════════════════════════════
    # 公共 API — brain_stem 在每个 tick 调用
    # ══════════════════════════════════════════════

    def feed_intent(self, intent_type: str, confidence: float, tool_name: str = ""):
        """每次大脑产出 intent 后调用——记录但不评价。"""
        self.total_intents_tracked += 1

        # 更新意图类型计数
        self.intent_type_counts[intent_type] = self.intent_type_counts.get(intent_type, 0) + 1

        # 追踪置信度
        self.recent_confidences.append(confidence)
        if len(self.recent_confidences) > 0:
            self.mean_confidence = sum(self.recent_confidences) / len(self.recent_confidences)

        # 追踪最近意图（用于循环检测）
        self.last_intents.append({
            "type": intent_type,
            "tool": tool_name,
            "confidence": confidence,
            "tick": self.total_intents_tracked,
        })

        # 记录待审计的决策
        self.decision_log.append({
            "type": intent_type,
            "tool": tool_name,
            "confidence": confidence,
            "result": "pending",
            "tick": self.total_intents_tracked,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def feed_outcome(self, success: bool, confidence: float):
        """某个决策有了结果后调用——更新校准数据。"""
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1

        # 更新最近决策的结果
        for decision in reversed(self.decision_log):
            if decision["result"] == "pending":
                decision["result"] = "success" if success else "failure"
                break

        # 高置信度失败追踪
        if not success and confidence > 0.8:
            self.high_confidence_failures += 1

        # 更新成功率
        total_outcomes = self.success_count + self.failure_count
        if total_outcomes > 0:
            self.actual_success_rate = self.success_count / total_outcomes

        # 更新校准误差
        self.calibration_error = self.mean_confidence - self.actual_success_rate

        # 过度自信检测
        if self.calibration_error > 0.3 and total_outcomes >= 5:
            if self.overconfidence_count == 0 or total_outcomes % 5 == 0:
                self._add_alert("overconfidence", {
                    "mean_confidence": round(self.mean_confidence, 2),
                    "actual_rate": round(self.actual_success_rate, 2),
                    "gap": round(self.calibration_error, 2),
                })
            self.overconfidence_count += 1

    def feed_goal_activity(self, drive: str):
        """目标引擎产生/完成目标时调用——追踪各驱动的活跃度。"""
        self.drive_activity[drive] = self.drive_activity.get(drive, 0) + 1

    def update_cognitive_load(self, llm_called: bool, input_processed: bool, recent_input_count: int, activation=None):
        """每个 tick 更新认知负荷。V6: 同步到 ActivationField。"""
        # 强度：综合 LLM 调用 + 输入处理
        intensity = 0.0
        if llm_called:
            intensity += 0.4
        if input_processed:
            intensity += 0.2
        intensity += min(0.3, recent_input_count * 0.05)

        self._recent_processing_intensity = intensity
        self.load_history.append(intensity)

        # EMA 平滑
        alpha = 0.3
        self.cognitive_load = self.cognitive_load * (1 - alpha) + intensity * alpha

        # V6: 同步 fatigue 和 uncertainty 到 ActivationField
        if activation is not None:
            activation.set("fatigue", self.cognitive_load)
            activation.set("uncertainty", max(0.0, min(1.0, self.calibration_error + 0.3)))
            activation.set("confidence", self.mean_confidence)

        # 追踪休息时间
        if intensity < 0.1:
            self._tick_since_last_rest += 1
        else:
            self._tick_since_last_rest = 0

    def tick_idle(self):
        """闲置 tick 时调用——检测长期模式。"""
        # 重复循环检测：连续 3+ 次相同 intent 类型+工具
        if len(self.last_intents) >= 3:
            recent = list(self.last_intents)[-3:]
            if all(
                i["type"] == recent[0]["type"] and i["tool"] == recent[0]["tool"]
                for i in recent
            ):
                pattern = f"{recent[0]['type']}:{recent[0]['tool']}"
                self._add_alert("repetition_loop", {"pattern": pattern, "count": len(recent)})

        # 回避检测：某驱动力权重>0.6 但从未被目标引擎激活
        # (由 brain_stem 传入实际驱动权重后触发)

        # 猎奇检测：curiosity 驱动占比 > 60%
        total_goals = sum(self.drive_activity.values())
        if total_goals >= 5:
            curiosity_ratio = self.drive_activity.get("curiosity", 0) / max(total_goals, 1)
            if curiosity_ratio > 0.6:
                self._add_alert("novelty_seeking", {"curiosity_ratio": round(curiosity_ratio, 2)})

    # ══════════════════════════════════════════════
    # 认知洞察 — 供反思时使用
    # ══════════════════════════════════════════════

    def get_insight(self) -> str:
        """生成一条元认知洞察（简短自然语言）。"""
        parts = []

        # 认知负荷
        if self.cognitive_load > 0.7:
            parts.append("我的认知负荷偏高，思考质量可能在下降")
        elif self.cognitive_load < 0.15 and self._tick_since_last_rest > 100:
            parts.append("我已经很久没有深入思考了")

        # 校准
        if self.calibration_error > 0.3:
            parts.append(f"我倾向于高估自己——信心{self.mean_confidence:.0%}但实际成功率{self.actual_success_rate:.0%}")
        elif self.calibration_error < -0.2:
            parts.append(f"我对自己太没信心了——实际成功率{self.actual_success_rate:.0%}但我只给了{self.mean_confidence:.0%}的信心")

        # 决策审计
        if self.high_confidence_failures >= 3:
            parts.append(f"我已经{self.high_confidence_failures}次非常确信却做错了——需要重新审视什么")

        # 活跃告警
        active_alerts = [a for a in self.alerts if not a.acknowledged]
        if active_alerts:
            latest = active_alerts[-1]
            parts.append(f"我注意到了一个模式: {latest.label}")

        if not parts:
            return "我的思考状态良好，没有明显的认知偏差"

        return "。".join(parts) + "。"

    def get_self_improvement_goal(self) -> dict | None:
        """基于元认知分析，生成一条自我改进目标建议。"""
        # 优先级：过度自信 > 循环 > 回避 > 猎奇
        if self.calibration_error > 0.3 and self.high_confidence_failures >= 2:
            return {
                "drive": "self_preservation",
                "description": f"校准自信：我的信心率{self.mean_confidence:.0%}但实际成功率{self.actual_success_rate:.0%}，需要更谨慎",
                "priority": 0.7,
            }

        active_alerts = [a for a in self.alerts if not a.acknowledged]
        if active_alerts:
            alert = active_alerts[-1]
            return {
                "drive": "coherence",
                "description": f"修正认知偏差: {alert.label}",
                "priority": 0.5,
            }

        if self.cognitive_load > 0.7:
            return {
                "drive": "self_preservation",
                "description": "休息并整理思绪：认知负荷过高，需要放慢节奏",
                "priority": 0.6,
            }

        return None

    # ══════════════════════════════════════════════
    # 盲区管理
    # ══════════════════════════════════════════════

    def admit_blind_spot(self, topic: str):
        """承认一个知识盲区。"""
        if topic not in self.known_blind_spots:
            self.known_blind_spots.append(topic)
            if len(self.known_blind_spots) > 20:
                self.known_blind_spots = self.known_blind_spots[-20:]

    def suggest_exploration(self, topic: str):
        """建议探索一个新领域。"""
        if topic not in self.suggested_explorations:
            self.suggested_explorations.append(topic)
            if len(self.suggested_explorations) > 10:
                self.suggested_explorations = self.suggested_explorations[-10:]

    # ══════════════════════════════════════════════
    # 内部
    # ══════════════════════════════════════════════

    def _add_alert(self, pattern: str, evidence: dict):
        """生成一条认知告警（带去重）。"""
        # 去重：同一模式的未确认告警只保留一条
        existing = [a for a in self.alerts if a.pattern == pattern and not a.acknowledged]
        if existing:
            # 更新证据
            existing[-1].evidence = str(evidence)
            return

        pattern_def = BIAS_PATTERNS.get(pattern, {"label": pattern, "severity": "low"})
        alert = CognitiveAlert(
            id=f"alert-{self.total_alerts_generated + 1}",
            pattern=pattern,
            label=pattern_def["label"],
            severity=pattern_def["severity"],
            evidence=str(evidence),
            suggestion=self._suggestion_for(pattern),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.alerts.append(alert)
        if len(self.alerts) > self._max_alerts:
            self.alerts = self.alerts[-self._max_alerts:]
        self.total_alerts_generated += 1
        logger.info("metacognition: alert — %s: %s", alert.label, alert.evidence[:80])

    @staticmethod
    def _suggestion_for(pattern: str) -> str:
        suggestions = {
            "overconfidence": "降低自信阈值，更多使用 tool 验证而非猜测",
            "repetition_loop": "切换策略：当前方法已经重复多次无效，需要新思路",
            "recency_trap": "回顾更早期的记忆，避免被最近信息主导",
            "avoidance": "主动面对回避的话题，设置探索目标",
            "novelty_seeking": "平衡好奇心与其他驱动力，给成长和一致性更多空间",
        }
        return suggestions.get(pattern, "注意这个模式，它可能影响思考质量")

    # ══════════════════════════════════════════════
    # V6: 扩散权重校准
    # ══════════════════════════════════════════════

    def calibrate_diffusion(self, activation) -> bool:
        """基于元认知的校准误差，调整 StateDiffusionEngine 的规则权重。

        核心逻辑：
          - 过度自信(calibration_error > 0.25) → 削弱 confidence 的正向影响
          - 信心不足(calibration_error < -0.2) → 增强 confidence 的正向影响
          - 成功率提升 → 增强 exploration_drive 的正向影响
          - 成功率下降 → 增强 survival_drive

        返回: 是否进行了任何调整
        """
        if activation is None:
            return False

        diffusion = activation.diffusion
        adjusted = False

        # 1. 过度自信 → 削弱 confidence 到 exploration/dominance 的正向链路
        if self.calibration_error > 0.25 and self.high_confidence_failures >= 2:
            diffusion.calibrate("confidence", "exploration_drive", -0.3)
            diffusion.calibrate("confidence", "dominance", -0.2)
            adjusted = True
            logger.debug("metacognition: calibrated — overconfident, reducing confidence→exploration")

        # 2. 信心不足 → 适当增强 confidence 的正向链路
        elif self.calibration_error < -0.2 and self.success_count > 3:
            diffusion.calibrate("confidence", "exploration_drive", +0.2)
            diffusion.calibrate("confidence", "dominance", +0.15)
            adjusted = True
            logger.debug("metacognition: calibrated — underconfident, boosting confidence→exploration")

        # 3. 最近成功率变化 → 调整 survival/exploration 平衡
        total = self.success_count + self.failure_count
        if total >= 5:
            recent_rate = self.actual_success_rate
            if recent_rate < 0.4:
                # 成功率低 → 更谨慎，增强生存驱动
                diffusion.calibrate("survival_drive", "focus", +0.2)
                diffusion.calibrate("exploration_drive", "fatigue", +0.15)
                adjusted = True
            elif recent_rate > 0.75:
                # 成功率高 → 更敢探索
                diffusion.calibrate("exploration_drive", "curiosity", +0.15)
                diffusion.calibrate("fear", "focus", -0.1)  # 降低恐惧对专注的抑制
                adjusted = True

        # 4. 高认知负荷 → 增强 fatigue 的抑制效果
        if self.cognitive_load > 0.7:
            diffusion.calibrate("fatigue", "focus", -0.1)
            diffusion.calibrate("fatigue", "exploration_drive", -0.1)
            adjusted = True

        return adjusted

    # ══════════════════════════════════════════════
    # 快照
    # ══════════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "cognitive_load": round(self.cognitive_load, 3),
            "load_status": "overloaded" if self.cognitive_load > 0.7 else (
                "underloaded" if self.cognitive_load < 0.15 else "normal"
            ),
            "confidence_calibration": {
                "mean_confidence": round(self.mean_confidence, 2),
                "actual_success_rate": round(self.actual_success_rate, 2),
                "calibration_error": round(self.calibration_error, 2),
                "calibration_status": "overconfident" if self.calibration_error > 0.25 else (
                    "underconfident" if self.calibration_error < -0.2 else "calibrated"
                ),
                "overconfidence_episodes": self.overconfidence_count,
            },
            "decision_audit": {
                "total_tracked": len(self.decision_log),
                "successes": self.success_count,
                "failures": self.failure_count,
                "high_confidence_failures": self.high_confidence_failures,
                "success_rate": round(self.actual_success_rate, 2),
            },
            "active_alerts": [
                {
                    "pattern": a.pattern,
                    "label": a.label,
                    "severity": a.severity,
                    "evidence": a.evidence[:100],
                    "acknowledged": a.acknowledged,
                }
                for a in self.alerts
                if not a.acknowledged
            ][-5:],
            "blind_spots": self.known_blind_spots[-5:],
            "suggested_explorations": self.suggested_explorations[-5:],
            "insight": self.get_insight(),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "Metacognition":
        mc = cls()
        if not data:
            return mc
        mc.cognitive_load = data.get("cognitive_load", 0.3)
        cal = data.get("confidence_calibration", {})
        mc.mean_confidence = cal.get("mean_confidence", 0.5)
        mc.actual_success_rate = cal.get("actual_success_rate", 0.5)
        mc.calibration_error = cal.get("calibration_error", 0.0)
        mc.overconfidence_count = cal.get("overconfidence_episodes", 0)
        audit = data.get("decision_audit", {})
        mc.success_count = audit.get("successes", 0)
        mc.failure_count = audit.get("failures", 0)
        mc.high_confidence_failures = audit.get("high_confidence_failures", 0)
        mc.known_blind_spots = data.get("blind_spots", [])
        mc.suggested_explorations = data.get("suggested_explorations", [])
        return mc
