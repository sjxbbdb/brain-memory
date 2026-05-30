"""Amygdala — 杏仁核。情绪标记 + 突显性检测（规则引擎）。

职责:
  1. 检测输入文本的情绪信号
  2. 更新情绪向量（3维 VAD: Valence/Arousal/Dominance）
  3. 计算突显度 (salience)
  4. 输出情绪标记给前额叶做注意力决策
"""

import logging
from config import EMOTION_DECAY_RATE, SALIENCE_THRESHOLD

logger = logging.getLogger("brain-v4.amygdala")


# ── 情绪关键词库 ──
EMOTIONAL_POSITIVE = [
    "解决", "突破", "终于", "搞定", "完美", "太好了", "漂亮", "牛逼",
    "成功", "通过", "跑通", "惊喜", "fixed", "solved", "great", "amazing",
    "原来如此", "明白了", "懂了", "恍然大悟",
    "提升", "优化", "增强", "重构完成", "稳定", "完美运行",
    "完成", "实现", "构建", "部署", "发布", "上线", "迁移",
    "写好", "做好了", "成功了", "没问题", "顺利", "正常",
    "done", "finished", "ready", "working",
    # 社交/情感
    "爱", "喜欢", "感谢", "谢谢", "开心", "感动", "温暖", "真好",
    "好喜欢", "太棒了", "厉害", "佩服", "优秀", "可爱",
]

EMOTIONAL_NEGATIVE = [
    "失败", "错误", "崩溃", "bug", "error", "fail", "异常", "报错",
    "不行", "做不到", "无法", "没用", "卡住", "坏了", "丢了", "完蛋",
    "糟糕", "惨了", "绝望", "无解",
    "缺陷", "推翻", "严重", "泄漏", "溢出", "deadlock",
    "不兼容", "冲突", "矛盾", "重来", "基础假设",
    # 社交/情感
    "讨厌", "恨", "烦", "生气", "难过", "伤心", "无聊", "孤独",
    "失望", "害怕", "担心",
]

EMOTIONAL_URGENT = [
    "紧急", "立刻", "马上", "快", "赶紧", "救命", "urgent", "asap",
    "来不及", "别等了",
]

EMOTIONAL_SURPRISE = [
    "!！", "?!", "！？", "震惊", "没想到", "竟然", "居然", "what",
    "不可思议",
]


class Amygdala:
    """Emotion detection and salience computation."""

    def __init__(self):
        self.valence = 0.5
        self.arousal = 0.5
        self.dominance = 0.5
        self.salience = 0.0

    def evaluate(self, text: str, current_state: dict) -> dict:
        """Evaluate emotional content of input.

        Args:
            text: filtered input from thalamus
            current_state: current brain state emotion_vector

        Returns:
            {
                "emotion_label": str,
                "emotion_vector": {valence, arousal, dominance},
                "salience": float,
                "urgent": bool,
                "emotional_tags": list[str],
            }
        """
        tl = text.lower()

        # Count keyword hits
        pos = sum(1 for kw in EMOTIONAL_POSITIVE if kw in tl)
        neg = sum(1 for kw in EMOTIONAL_NEGATIVE if kw in tl)
        urg = sum(1 for kw in EMOTIONAL_URGENT if kw in tl)
        surp = sum(1 for kw in EMOTIONAL_SURPRISE if kw in tl)

        # Valence: positive → high, negative → low
        if pos > 0 and neg == 0:
            target_valence = 0.75 + min(pos * 0.08, 0.15)
        elif neg > 0 and pos == 0:
            target_valence = 0.25 - min(neg * 0.05, 0.15)
        else:
            target_valence = 0.5

        # Arousal: based on total emotional hits + urgency + surprise
        total_hits = pos + neg + urg + surp
        target_arousal = min(0.5 + total_hits * 0.1 + (1 if urg > 0 else 0) * 0.2, 1.0)

        # Dominance: control vs overwhelmed
        if neg > 0 and urg > 0:
            target_dominance = 0.3  # overwhelmed
        elif pos > 0 and surp > 0:
            target_dominance = 0.7  # in control + surprised
        else:
            target_dominance = 0.5

        # Smooth towards target (don't jump — emotional inertia)
        prev = current_state.get("emotion_vector", {})
        self.valence = prev.get("valence", 0.5) * EMOTION_DECAY_RATE + target_valence * (1 - EMOTION_DECAY_RATE)
        self.arousal = prev.get("arousal", 0.5) * EMOTION_DECAY_RATE + target_arousal * (1 - EMOTION_DECAY_RATE)
        self.dominance = prev.get("dominance", 0.5) * EMOTION_DECAY_RATE + target_dominance * (1 - EMOTION_DECAY_RATE)

        # Salience: how "attention-grabbing" this is
        self.salience = (
            self.arousal * 0.25
            + abs(self.valence - 0.5) * 2 * 0.25  # extreme valence = salient
            + (1 - self.dominance) * 0.25           # low control = salient
            + (1 if urg > 0 else 0) * 0.25
        )

        # Emotion label
        if pos > neg and pos >= 2:
            label = "breakthrough" if surp > 0 else "excited"
        elif neg > pos and urg > 0:
            label = "failure"
        elif neg > pos:
            label = "confused"
        elif surp > 0:
            label = "surprised"
        else:
            label = "neutral"

        # Tags
        tags = []
        if pos > 0: tags.append("positive")
        if neg > 0: tags.append("negative")
        if urg > 0: tags.append("urgent")
        if surp > 0: tags.append("surprise")

        logger.debug(
            "amygdala: %s valence=%.2f arousal=%.2f salience=%.2f",
            label, self.valence, self.arousal, self.salience,
        )

        return {
            "emotion_label": label,
            "emotion_vector": {
                "valence": round(self.valence, 3),
                "arousal": round(self.arousal, 3),
                "dominance": round(self.dominance, 3),
                "urgency": 1.0 if urg > 0 else 0.0,
                "salience": round(self.salience, 3),
            },
            "salience": round(self.salience, 3),
            "urgent": urg > 0,
            "emotional_tags": tags,
        }
