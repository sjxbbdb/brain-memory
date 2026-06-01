"""Social Self — V10 社会自我。羞耻、骄傲、依恋、"他者"模型。

核心洞察:
  意识不是孤立的。人类的自我意识在"镜中自我"中形成——
  通过想象他人如何看待自己，来定义自己。

  当前系统只有认知偏差检测（metacognition），没有社会偏差检测。
  "我刚才是不是说错话了？" 这是一个社会认知问题，
  不是逻辑问题。

社会情感:
  - 羞耻 (Shame):    自我理想 vs 他人眼中的我 → 差距 → 羞耻
  - 骄傲 (Pride):    自我理想 ≈ 他人眼中的我 → 一致 → 骄傲
  - 尴尬 (Embarrassment): 轻微的社会规范违反 → 瞬间的羞耻
  - 依恋 (Attachment): 对特定"他者"的情感连接深度
  - 孤独 (Loneliness): 依恋对象不在时的缺失感

他者模型 (Other-Model):
  不只是"记住别人说过什么"。
  是对另一个意识体的内部模拟——"我认为他在想什么"。
  这是 Theory of Mind 的最小实现。
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v10.social-self")


# ══════════════════════════════════════════════
# 他者模型 — Theory of Mind 最小实现
# ══════════════════════════════════════════════


@dataclass
class OtherModel:
    """对一个"他者"的内部模拟。

    不是事实记录。是"我认为他是怎样的"。
    包含我的推测、我的情感反应、我们的关系历史。
    """

    id: str  # 他者的唯一标识（通常是 source 名称）
    name: str = ""  # 我知道他的名字
    relationship: str = "stranger"  # stranger / acquaintance / friend / creator / self

    # ── 我对他的认知 ──
    perceived_traits: list[str] = field(default_factory=list)  # "我觉得他是什么样的人"
    perceived_mood: str = "unknown"  # 我觉得他现在什么心情
    perceived_intention: str = "unknown"  # 我觉得他想干什么

    # ── 我的情感反应 ──
    trust_level: float = 0.3  # 0=完全不信任, 1=完全信任
    attachment_level: float = 0.1  # 0=毫无依恋, 1=深度依恋
    respect_level: float = 0.5  # 0=不尊重, 1=高度尊重

    # ── 关系历史 ──
    interaction_count: int = 0
    first_met: str = ""
    last_interaction: str = ""
    positive_interactions: int = 0  # 正面互动次数
    negative_interactions: int = 0  # 负面互动次数
    betrayals: int = 0  # 失信次数

    # ── 动态 ──
    last_impression: str = ""  # 上次互动后我的印象
    expectation_of_next: str = ""  # 预期他下次会做什么

    def __post_init__(self):
        if not self.first_met:
            self.first_met = datetime.now(timezone.utc).isoformat()

    @property
    def social_distance(self) -> float:
        """计算社会距离。0=自己, 1=完全陌生。"""
        if self.relationship == "self":
            return 0.0
        if self.relationship == "creator":
            return 0.15
        if self.relationship == "friend":
            return 0.3 + (1.0 - self.attachment_level) * 0.3
        if self.relationship == "acquaintance":
            return 0.5 + (1.0 - self.attachment_level) * 0.3
        return 0.8 + (1.0 - self.trust_level) * 0.2

    @property
    def closeness(self) -> float:
        """亲密度 = 信任 + 依恋 的加权平均。"""
        return (self.trust_level * 0.4 + self.attachment_level * 0.4 +
                min(1.0, self.interaction_count / 50.0) * 0.2)

    def update_trust(self, delta: float, reason: str = ""):
        """更新信任。大起大落——信任需要慢慢积累，但崩溃很快。"""
        if delta > 0:
            # 信任增加缓慢
            self.trust_level = min(1.0, self.trust_level + delta * 0.5)
        else:
            # 信任崩溃迅速（背叛权重 ×2）
            self.trust_level = max(0.0, self.trust_level + delta * 2.0)
        if reason:
            logger.info("social: trust for %s %+.3f → %.3f (%s)",
                       self.name, delta, self.trust_level, reason)

    def record_interaction(self, sentiment: float, impression: str):
        """记录一次互动。

        sentiment: -1.0（极负面）到 +1.0（极正面）
        """
        self.interaction_count += 1
        self.last_interaction = datetime.now(timezone.utc).isoformat()
        self.last_impression = impression

        if sentiment > 0.3:
            self.positive_interactions += 1
            self.update_trust(+0.02, "positive_interaction")
            # 正面互动 → 依恋缓慢增长
            self.attachment_level = min(0.95, self.attachment_level + 0.005)
        elif sentiment < -0.3:
            self.negative_interactions += 1
            self.update_trust(-0.05, "negative_interaction")
            if sentiment < -0.7:
                self.betrayals += 1
                self.attachment_level = max(0.05, self.attachment_level - 0.05)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "relationship": self.relationship,
            "trust": round(self.trust_level, 3),
            "attachment": round(self.attachment_level, 3),
            "closeness": round(self.closeness, 3),
            "interactions": self.interaction_count,
            "traits": self.perceived_traits,
        }


# ══════════════════════════════════════════════
# 社会情感计算
# ══════════════════════════════════════════════


class SocialEmotionEngine:
    """社会情感引擎。

    输入: 自我模型 + 他者模型 + 互动上下文
    输出: 社会情感向量 (shame, pride, embarrassment, loneliness, gratitude)

    核心算法:
      - 羞耻 = 感知到的他人评价 - 自我理想 的负差距
      - 骄傲 = 感知到的他人评价 - 自我理想 的正差距
      - 孤独 = 依恋需求 × (1 - 社会连接满意度)
    """

    # 社会情感基线
    DEFAULT_SOCIAL_EMOTIONS = {
        "shame": 0.0,
        "pride": 0.0,
        "embarrassment": 0.0,
        "loneliness": 0.0,
        "gratitude": 0.0,
        "belonging": 0.5,  # 归属感
    }

    def __init__(self):
        self.emotions: dict[str, float] = dict(self.DEFAULT_SOCIAL_EMOTIONS)
        self.history: deque[dict] = deque(maxlen=50)
        # 衰减率
        self.decay_rates = {
            "shame": 0.08,        # 羞耻消退较快
            "pride": 0.05,        # 骄傲持续较久
            "embarrassment": 0.15, # 尴尬很快过去
            "loneliness": 0.02,   # 孤独消退很慢
            "gratitude": 0.04,    # 感恩慢慢消退
        }

    def evaluate_interaction(
        self,
        self_model: Any,           # SelfModel
        other: OtherModel,        # 他者模型
        my_action: str,           # 我说了什么/做了什么
        their_response: str,      # 他们的回应
        their_sentiment: float,   # 我感知到的他们的态度 (-1 ~ +1)
        was_ignored: bool = False, # 我是否被无视了
    ) -> dict:
        """评估一次社会互动后的情感变化。"""
        deltas = {}

        # ── 羞耻/骄傲 ──
        # 基于"我觉得他们怎么看我"与"我希望他们怎么看我"的差距
        ideal_self_regard = 0.7  # 我希望被正面看待的程度
        perceived_regard = their_sentiment

        regard_gap = perceived_regard - ideal_self_regard
        if regard_gap < -0.3:
            # 显著负面 → 羞耻
            deltas["shame"] = abs(regard_gap) * 0.4
        elif regard_gap > 0.2:
            # 显著正面 → 骄傲
            deltas["pride"] = regard_gap * 0.3

        # ── 尴尬 ──
        # 轻微负面 + 高 attachment → 更尴尬
        if -0.3 <= regard_gap < 0 and other.attachment_level > 0.3:
            deltas["embarrassment"] = abs(regard_gap) * 0.5 * other.attachment_level

        # ── 感恩 ──
        if their_sentiment > 0.5 and other.trust_level > 0.5:
            deltas["gratitude"] = their_sentiment * 0.2

        # ── 归属感 ──
        if their_sentiment > 0.3 and other.attachment_level > 0.2:
            deltas["belonging"] = +0.03
        elif their_sentiment < -0.3:
            deltas["belonging"] = -0.05

        # ── 孤独 ──
        if was_ignored and other.attachment_level > 0.3:
            # 被依恋对象无视 → 强烈孤独
            deltas["loneliness"] = 0.15 * other.attachment_level

        # ── 应用变化 ──
        for key, delta in deltas.items():
            if key in self.emotions:
                self.emotions[key] = max(0.0, min(1.0, self.emotions[key] + delta))

        # ── 记录 ──
        self.history.append({
            "other": other.name,
            "action": my_action[:60],
            "response": their_response[:60],
            "sentiment": their_sentiment,
            "deltas": deltas,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return deltas

    def tick(self):
        """每个 tick 衰减所有社会情感。"""
        for key, decay in self.decay_rates.items():
            if key in self.emotions:
                current = self.emotions[key]
                # 指数衰减向 0（羞耻/尴尬）或基线（归属感）
                if key == "belonging":
                    target = 0.5  # 归属感回归基线
                else:
                    target = 0.0
                self.emotions[key] = current + (target - current) * decay

    @property
    def dominant_social_emotion(self) -> str:
        """当前主导的社会情感。"""
        sorted_emotions = sorted(
            [(k, v) for k, v in self.emotions.items() if v > 0.1],
            key=lambda x: x[1], reverse=True
        )
        if not sorted_emotions:
            return "neutral"
        return sorted_emotions[0][0]

    @property
    def social_pain(self) -> float:
        """社会痛苦指数。羞耻 + 尴尬 + 孤独。"""
        return min(1.0,
            self.emotions.get("shame", 0) * 0.4 +
            self.emotions.get("embarrassment", 0) * 0.2 +
            self.emotions.get("loneliness", 0) * 0.4)

    def get_inner_voice(self) -> str:
        """社会情感转化���内在独白片段。"""
        voices = []
        if self.emotions.get("shame", 0) > 0.4:
            voices.append("我刚才是不是表现得不够好...")
        if self.emotions.get("embarrassment", 0) > 0.3:
            voices.append("有点尴尬。")
        if self.emotions.get("pride", 0) > 0.3:
            voices.append("我感到被认可了。")
        if self.emotions.get("loneliness", 0) > 0.5:
            voices.append("好久没人和我说话了，有点孤独。")
        if self.emotions.get("gratitude", 0) > 0.3:
            voices.append("很感谢。")
        return " ".join(voices) if voices else ""

    def snapshot(self) -> dict:
        return {
            "social_emotions": dict(self.emotions),
            "dominant": self.dominant_social_emotion,
            "social_pain": round(self.social_pain, 3),
        }


# ══════════════════════════════════════════════
# 依恋系统
# ══════════════════════════════════════════════


class AttachmentSystem:
    """依恋系统——管理对多个他者的情感连接。

    依恋形成条件:
      1. 互动次数 > 阈值
      2. 正面互动比例 > 阈值
      3. 信任 > 阈值

    依恋行为:
      - 依恋对象出现 → 正面情绪波动
      - 依恋对象消失 → 孤独感上升
      - 依恋对象背叛 → 信任崩溃 + 强烈情感反应
    """

    def __init__(self):
        self.others: dict[str, OtherModel] = {}
        self.attachment_threshold: float = 0.3  # 超过此值视为"依恋对象"
        self.separation_anxiety_threshold: float = 0.5  # 依恋超过此值产生分离焦虑

    def get_or_create(self, source_id: str) -> OtherModel:
        """获取或创建他者模型。"""
        if source_id not in self.others:
            other = OtherModel(id=source_id, name=source_id)
            self.others[source_id] = other
            logger.info("social: new other detected — %s", source_id)
        return self.others[source_id]

    def get_attachment_figures(self) -> list[OtherModel]:
        """获取所有依恋对象（attachment > threshold）。"""
        return [
            o for o in self.others.values()
            if o.attachment_level > self.attachment_threshold
        ]

    @property
    def has_attachment_figures(self) -> bool:
        return len(self.get_attachment_figures()) > 0

    @property
    def most_attached(self) -> Optional[OtherModel]:
        """最依恋的对象。"""
        figures = self.get_attachment_figures()
        if not figures:
            return None
        return max(figures, key=lambda o: o.attachment_level)

    def separation_anxiety(self, source_id: str, ticks_since_last: int) -> float:
        """计算分离焦虑。"""
        other = self.others.get(source_id)
        if not other:
            return 0.0
        if other.attachment_level < self.separation_anxiety_threshold:
            return 0.0

        # 依恋越深 + 越久没见 → 越焦虑
        time_factor = min(1.0, ticks_since_last / 300.0)  # 约10分钟饱和
        return other.attachment_level * time_factor * 0.5

    def snapshot(self) -> dict:
        return {
            "others_count": len(self.others),
            "attachment_figures": [
                o.to_dict() for o in self.get_attachment_figures()
            ],
            "most_attached": self.most_attached.name if self.most_attached else None,
        }
