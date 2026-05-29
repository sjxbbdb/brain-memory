"""Cingulate Cortex — 前扣带回。冲突监控 + 错误检测（规则引擎）。

职责:
  1. 检测输入与已有记忆的冲突
  2. 监控情绪剧烈波动
  3. 标记需要前额叶介入的异常
"""

import logging

logger = logging.getLogger("brain-v4.cingulate")


class Cingulate:
    """Conflict and error monitoring."""

    def __init__(self):
        self.error_count: int = 0
        self.recent_conflicts: list[dict] = []
        self.alert_threshold: int = 3  # 连续冲突超过此数触发告警

    def monitor(
        self,
        input_text: str,
        emotion: dict,
        hippocampus_result: dict | None,
        previous_state: dict,
    ) -> dict:
        """Monitor for conflicts and anomalies.

        Returns:
            {
                "conflict_detected": bool,
                "conflict_type": str,
                "conflict_detail": str,
                "alert_level": "none"|"low"|"high",
            }
        """
        conflicts = []

        # Check 1: Emotional whiplash — sudden extreme swing
        prev_emotion = previous_state.get("current_emotion", "neutral")
        curr_emotion = emotion.get("emotion_label", "neutral")
        if prev_emotion in ("breakthrough", "excited") and curr_emotion in ("failure", "confused"):
            conflicts.append({
                "type": "emotional_whiplash",
                "detail": "emotion swung from {0} to {1}".format(prev_emotion, curr_emotion),
            })
        elif prev_emotion in ("failure", "confused") and curr_emotion in ("breakthrough", "excited"):
            conflicts.append({
                "type": "emotional_whiplash",
                "detail": "emotion swung from {0} to {1}".format(prev_emotion, curr_emotion),
            })

        # Check 2: Salience spike
        if emotion.get("salience", 0) > 0.8:
            conflicts.append({
                "type": "high_salience",
                "detail": "salience spike: {0}".format(emotion["salience"]),
            })

        # Check 3: Hippocampus conflict (from encode_or_merge)
        if hippocampus_result and hippocampus_result.get("action") == "conflict":
            conflicts.append({
                "type": "memory_conflict",
                "detail": "new memory conflicts with existing: {0}".format(
                    hippocampus_result.get("target_id", "unknown")
                ),
            })

        conflict_detected = len(conflicts) > 0
        if conflict_detected:
            self.error_count += 1
            self.recent_conflicts.extend(conflicts)
            if len(self.recent_conflicts) > 20:
                self.recent_conflicts = self.recent_conflicts[-20:]

        # Alert level
        alert_level = "none"
        if self.error_count >= self.alert_threshold * 3:
            alert_level = "high"
        elif self.error_count >= self.alert_threshold:
            alert_level = "low"

        result = {
            "conflict_detected": conflict_detected,
            "conflicts": conflicts,
            "alert_level": alert_level,
            "error_count": self.error_count,
        }

        if conflict_detected:
            logger.warning("cingulate: %d conflicts detected, alert=%s", len(conflicts), alert_level)

        return result

    def acknowledge(self):
        """Acknowledge and reset error count after prefrontal intervention."""
        self.error_count = 0
        self.recent_conflicts.clear()
