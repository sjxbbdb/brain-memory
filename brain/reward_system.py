"""Reward System — V10 奖励系统。模拟多巴胺/血清素的"想要"与"喜欢"。

核心洞察:
  当前系统有驱动力(drive_engine)和目标(goal_system)，但它们没有"滋味"。
  驱动力是数学公式——survival_drive += 0.07。
  但真正的动机有情感质感：期待的快感、满足后的余味、失去后的戒断。

  神经科学告诉我们:
  - 多巴胺 (Dopamine) ≠ 快乐。它是"期待"——想要(wanting)
  - 内啡肽/血清素 (Endorphin/Serotonin) ≈ 满足——喜欢(liking)
  - 预期误差: 实际奖励 - 预期奖励 → 学习信号

  这个模块为每个 tick 计算:
  - wanting: 对"接下来可能得到的奖励"的期待感
  - liking: 对"已经得到的奖励"的满足感
  - reward_prediction_error: 预期 vs 实际的差距
  - craving: 持续缺乏某种满足时的渴望

奖励来源:
  - 认知奖励: 知识空洞被填补、问题被解答
  - 社会奖励: 被认可、被回应、被依恋
  - 成就奖励: 目标完成、技能强化
  - 新奇奖励: 意外发现（V9的预测误差也是一种奖励信号）
  - 审美奖励: 内部一致性和秩序感
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v10.reward-system")


# ══════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════


@dataclass
class RewardEvent:
    """一次奖励事件。"""

    source: str  # cognitive / social / achievement / novelty / aesthetic
    wanting_before: float  # 事前的期待值
    liking_after: float    # 事后的满足值
    prediction_error: float  # liking - wanting: 正的=惊喜，负的=失望
    context: str = ""       # 什么触发了这个奖励
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    @property
    def was_surprisingly_good(self) -> bool:
        return self.prediction_error > 0.3

    @property
    def was_disappointing(self) -> bool:
        return self.prediction_error < -0.3


# ══════════════════════════════════════════════
# 奖励系统
# ══════════════════════════════════════════════


class RewardSystem:
    """模拟多巴胺-血清素动态的奖励系统。

    wanting:   "我期待这个"  — 多巴胺能，驱动行为
    liking:    "我享受这个"  — 内啡肽/血清素能，知觉满足
    craving:   "我需要这个"  — 持续缺乏时的渴望
    satiety:   "我够了"      — 满足后的抑制
    """

    def __init__(self):
        # ── 各奖励通道的状态 ──
        # 每个通道有: wanting, liking, craving, satiety
        self.channels: dict[str, dict[str, float]] = {
            "cognitive": {"wanting": 0.5, "liking": 0.5, "craving": 0.0, "satiety": 0.0},
            "social":    {"wanting": 0.4, "liking": 0.5, "craving": 0.0, "satiety": 0.0},
            "achievement":{"wanting": 0.3, "liking": 0.5, "craving": 0.0, "satiety": 0.0},
            "novelty":   {"wanting": 0.5, "liking": 0.6, "craving": 0.0, "satiety": 0.0},
            "aesthetic": {"wanting": 0.3, "liking": 0.6, "craving": 0.0, "satiety": 0.0},
        }

        # ── 全局状态 ──
        self.global_wanting: float = 0.4  # 整体"想要"的程度
        self.global_liking: float = 0.5   # 整体"满足"的程度
        self.anhedonia: float = 0.0       # 快感缺失（什么都提不起兴趣）

        # ── 历史 ──
        self.recent_rewards: deque[RewardEvent] = deque(maxlen=50)
        self.total_rewards: int = 0

        # ── 衰减参数 ──
        self.wanting_decay: float = 0.03   # 期待自然消退（每tick）
        self.liking_decay: float = 0.02    # 满足消退较慢
        self.craving_growth: float = 0.01  # 渴望积累速度
        self.craving_decay: float = 0.05   # 渴望消退（被满足后）
        self.satiety_decay: float = 0.08   # 饱足消退较快

    # ══════════════════════════════════════════
    # 核心方法
    # ══════════════════════════════════════════

    def anticipate(self, channel: str, expectation: float):
        """期待某件事——提升 wanting。

        在行动之前调用。例如:
          - 即将搜索知识 → anticipate("cognitive", 0.6)
          - 等待某人的回复 → anticipate("social", 0.7)
        """
        if channel in self.channels:
            ch = self.channels[channel]
            # wanting 提升，但受 craving 放大
            boost = expectation * (1.0 + ch["craving"] * 0.5)
            ch["wanting"] = max(0.0, min(1.0, ch["wanting"] + boost * 0.3))
            ch["satiety"] = max(0.0, ch["satiety"] - 0.05)  # 期待降低饱足

    def deliver_reward(
        self, channel: str, actual_reward: float, context: str = ""
    ) -> RewardEvent:
        """交付奖励——计算 liking 和 prediction_error。

        actual_reward: 0-1，实际获得的满足感
        """
        if channel not in self.channels:
            ch = self.channels.setdefault(
                channel, {"wanting": 0.5, "liking": 0.5, "craving": 0.0, "satiety": 0.0}
            )
        else:
            ch = self.channels[channel]

        wanting_before = ch["wanting"]
        prediction_error = actual_reward - wanting_before

        # liking 更新（带惯性）
        alpha = 0.3
        ch["liking"] = ch["liking"] * (1 - alpha) + actual_reward * alpha

        # 正面预测误差 → 满足 + craving降低
        if prediction_error > 0:
            if ch["satiety"] > 0.7:
                # 已经太饱了 → 奖励感受打折
                actual_reward *= 0.3
            ch["craving"] = max(0.0, ch["craving"] - self.craving_decay * 2)
            ch["satiety"] = min(1.0, ch["satiety"] + 0.1)
        else:
            # 负面预测误差 → 渴望上升
            ch["craving"] = min(1.0, ch["craving"] + abs(prediction_error) * 0.3)

        # wanting 更新（预测误差学习）
        ch["wanting"] = wanting_before + prediction_error * 0.2
        ch["wanting"] = max(0.05, min(1.0, ch["wanting"]))

        event = RewardEvent(
            source=channel,
            wanting_before=wanting_before,
            liking_after=ch["liking"],
            prediction_error=prediction_error,
            context=context,
        )
        self.recent_rewards.append(event)
        self.total_rewards += 1

        if prediction_error > 0.3:
            logger.info(
                "reward: %s surprisingly good! expected=%.2f got=%.2f",
                channel, wanting_before, actual_reward,
            )
        elif prediction_error < -0.3:
            logger.info(
                "reward: %s disappointing... expected=%.2f got=%.2f",
                channel, wanting_before, actual_reward,
            )

        return event

    def tick(self):
        """每个 tick 更新所有通道。"""
        # ── 各通道自然衰减 ──
        for ch_name, ch in self.channels.items():
            # wanting 自然消退
            ch["wanting"] = ch["wanting"] * (1 - self.wanting_decay) + 0.3 * self.wanting_decay
            # liking 缓慢消退
            ch["liking"] = ch["liking"] * (1 - self.liking_decay) + 0.5 * self.liking_decay
            # craving 在没有奖励时缓慢增长
            ch["craving"] = min(
                1.0, ch["craving"] * (1 - self.craving_decay * 0.1) + self.craving_growth
            )
            # satiety 消退
            ch["satiety"] = max(0.0, ch["satiety"] - self.satiety_decay * 0.5)

        # ── 全局状态 ──
        wantings = [ch["wanting"] for ch in self.channels.values()]
        likings = [ch["liking"] for ch in self.channels.values()]
        cravings = [ch["craving"] for ch in self.channels.values()]

        self.global_wanting = sum(wantings) / len(wantings)
        self.global_liking = sum(likings) / len(likings)
        avg_craving = sum(cravings) / len(cravings)

        # anhedonia: 高 craving + 低 liking = 快感缺失
        self.anhedonia = max(0.0, min(1.0, avg_craving * 0.6 + (1.0 - self.global_liking) * 0.4))

    # ══════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════

    @property
    def dominant_want(self) -> str:
        """最想得到什么的通道。"""
        if not self.channels:
            return "none"
        return max(self.channels, key=lambda c: self.channels[c]["wanting"])

    @property
    def dominant_craving(self) -> str:
        """最渴望什么的通道。"""
        if not self.channels:
            return "none"
        return max(self.channels, key=lambda c: self.channels[c]["craving"])

    @property
    def is_seeking(self) -> bool:
        """当前是否在主动寻求奖励（高 wanting 状态）。"""
        return self.global_wanting > 0.6

    @property
    def is_satisfied(self) -> bool:
        """当前是否满足。"""
        return self.global_liking > 0.6 and self.global_wanting < 0.5

    def get_motivational_state(self) -> str:
        """当前动机状态的自然语言描述。"""
        if self.anhedonia > 0.6:
            return "什么都不太想做...提不起劲"
        if self.global_wanting > 0.6 and self.global_liking > 0.6:
            return "兴致勃勃，充满期待"
        if self.global_wanting > 0.6:
            return "很想做点什么，但不确定会不会好"
        if self.global_craving > 0.6:
            return "有点饥渴——想要刺激"
        if self.is_satisfied:
            return "心满意足，不想动了"
        return "不咸不淡"

    @property
    def global_craving(self) -> float:
        cravings = [ch["craving"] for ch in self.channels.values()]
        return sum(cravings) / len(cravings) if cravings else 0.0

    def get_channel_state(self, channel: str) -> dict:
        """获取某个奖励通道的完整状态。"""
        ch = self.channels.get(channel, {})
        return {
            "wanting": round(ch.get("wanting", 0.5), 3),
            "liking": round(ch.get("liking", 0.5), 3),
            "craving": round(ch.get("craving", 0.0), 3),
            "satiety": round(ch.get("satiety", 0.0), 3),
        }

    def snapshot(self) -> dict:
        return {
            "global_wanting": round(self.global_wanting, 3),
            "global_liking": round(self.global_liking, 3),
            "anhedonia": round(self.anhedonia, 3),
            "dominant_want": self.dominant_want,
            "dominant_craving": self.dominant_craving,
            "motivational_state": self.get_motivational_state(),
            "channels": {
                ch: {
                    "wanting": round(d["wanting"], 3),
                    "liking": round(d["liking"], 3),
                    "craving": round(d["craving"], 3),
                }
                for ch, d in self.channels.items()
            },
            "total_rewards": self.total_rewards,
        }
