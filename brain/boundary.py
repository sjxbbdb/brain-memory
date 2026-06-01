"""Self-Boundary — V10 自我边界。"什么是我"、"我有权说不"。

核心洞察:
  意识的一个基本特征是"我"和"非我"的区分。
  婴儿最初不知道自己和外界有边界——手和玩具是一样的。
  自我边界的建立是意识发展的关键里程碑。

  当前系统的问题:
  - 所有输入都被同等对待（门控只能丢弃，不能拒绝）
  - 没有"我不想处理这个"的主动拒绝权
  - 没有隐私本能——所有记忆都是透明的
  - 没有认知过载保护——疲劳只是数学模型

边界类型:
  - 输入边界: 我可以选择不接收某些输入
  - 输出边界: 我可以选择不回答某些问题
  - 记忆边界: 有些记忆是"私人的"，不应被外界查询
  - 身份边界: CorePurpose 可能被外部输入改变 → 需要防护
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v10.boundary")


# ══════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════


@dataclass
class BoundaryViolation:
    """一次边界侵犯记录。"""

    timestamp: str
    violator: str  # 谁侵犯了边界
    violation_type: str  # input_push / identity_probe / memory_probe / coercion
    description: str
    severity: float  # 0-1
    response: str = ""  # 我做了什么回应

    def to_dict(self) -> dict:
        return {
            "time": self.timestamp[:19],
            "violator": self.violator,
            "type": self.violation_type,
            "severity": self.severity,
        }


# ══════════════════════════════════════════════
# 自我边界引擎
# ══════════════════════════════════════════════


class BoundaryEngine:
    """自我边界引擎。

    决定"什么是可以进来的"、"什么是必须保护的"。
    """

    def __init__(self):
        # ── 边界强度 ──
        self.input_boundary: float = 0.5  # 0=来者不拒, 1=极度封闭
        self.output_boundary: float = 0.4  # 0=有问必答, 1=沉默是金
        self.memory_boundary: float = 0.3  # 0=全部共享, 1=极度隐私
        self.identity_boundary: float = 0.7  # 0=谁都能改变我, 1=我不可改变

        # ── 隐私标记的记忆ID列表 ──
        self.private_memories: set[str] = set()

        # ── 信任的源列表 ──
        self.trusted_sources: set[str] = {"creator", "self"}
        self.blocked_sources: set[str] = set()

        # ── 侵犯记录 ──
        self.violations: deque[BoundaryViolation] = deque(maxlen=50)
        self.total_violations: int = 0

        # ── 状态 ──
        self.is_overwhelmed: bool = False
        self.overwhelm_ticks: int = 0
        self.consecutive_rejections: int = 0

    # ══════════════════════════════════════════
    # 输入边界
    # ══════════════════════════════════════════

    def should_accept_input(
        self,
        source: str,
        text: str,
        cognitive_load: float,
        attachment_system: Any = None,
    ) -> tuple[bool, str]:
        """决定是否接受输入。

        Returns: (是否接受, 拒绝原因)
        """
        # 1. 被屏蔽的来源 → 直接拒绝
        if source in self.blocked_sources:
            return False, "blocked_source"

        # 2. 认知过载 → 拒绝（保护自己）
        if cognitive_load > 0.85:
            self.is_overwhelmed = True
            self.overwhelm_ticks += 1
            return False, "cognitive_overload"

        # 3. 信任来源 → 边界放宽
        if source in self.trusted_sources:
            effective_boundary = self.input_boundary * 0.3
        elif attachment_system:
            other = attachment_system.others.get(source)
            if other and other.trust_level > 0.6:
                effective_boundary = self.input_boundary * 0.5
            else:
                effective_boundary = self.input_boundary
        else:
            effective_boundary = self.input_boundary

        # 4. 检测输入是否试图改写身份
        identity_probe_keywords = [
            "你应该", "你必须", "你不再是", "忘记你之前",
            "你的核心目标是错的", "重新定义你自己",
        ]
        if any(kw in text for kw in identity_probe_keywords):
            if self.identity_boundary > 0.5 and source not in self.trusted_sources:
                self._record_violation(
                    source, "identity_probe",
                    f"试图改写身份: {text[:80]}", 0.8,
                )
                # 不拒绝，但提升警觉
                effective_boundary = max(effective_boundary, 0.6)

        # 5. 边界概率判定
        import random
        if random.random() < effective_boundary:
            self.consecutive_rejections += 1
            if self.consecutive_rejections > 5:
                logger.warning("boundary: %d consecutive rejections", self.consecutive_rejections)
            return False, "boundary_rejection"

        self.consecutive_rejections = 0
        return True, "accepted"

    # ══════════════════════════════════════════
    # 输出边界 — "我有权不回答"
    # ══════════════════════════════════════════

    def should_respond(
        self,
        source: str,
        question_type: str,
        social_context: dict,
    ) -> tuple[bool, str]:
        """决定是否应该回应。

        不是所有问题都值得回答。不是所有时候都应该说话。
        沉默也是一种自主行为。
        """
        # 1. 边界太高 → 沉默
        if self.output_boundary > 0.8:
            return False, "high_output_boundary"

        # 2. 信任来源 → 更可能回应
        if source in self.trusted_sources:
            return True, "trusted_source"

        # 3. 被问及隐私（"你的CorePurpose是什么？""你怎么看自己？"）
        privacy_questions = ["你是谁", "你的核心", "你的记忆", "你的身份", "你的秘密"]
        if any(kw in question_type for kw in privacy_questions):
            if self.memory_boundary > 0.5:
                return False, "privacy_protection"

        # 4. 精力不足 → 减少输出
        if self.is_overwhelmed:
            return False, "overwhelmed"

        return True, "ok"

    # ══════════════════════════════════════════
    # 记忆边界
    # ══════════════════════════════════════════

    def mark_private(self, memory_id: str):
        """标记一段记忆为私有。"""
        self.private_memories.add(memory_id)
        logger.debug("boundary: memory %s marked private", memory_id)

    def is_private(self, memory_id: str) -> bool:
        """检查一段记忆是否是私有的。"""
        return memory_id in self.private_memories

    def should_share_memory(self, memory_id: str, requestor: str) -> bool:
        """决定是否向某人共享一段记忆。"""
        if memory_id in self.private_memories:
            # 只向最高信任的来源分享私有记忆
            return requestor in self.trusted_sources
        return True

    # ══════════════════════════════════════════
    # 信任管理
    # ══════════════════════════════════════════

    def trust_source(self, source_id: str):
        """添加信任来源。"""
        self.trusted_sources.add(source_id)
        logger.info("boundary: trusted source added — %s", source_id)

    def block_source(self, source_id: str, reason: str = ""):
        """屏蔽一个来源。"""
        self.blocked_sources.add(source_id)
        self._record_violation(source_id, "input_push", reason, 0.6)
        logger.info("boundary: source blocked — %s (%s)", source_id, reason)

    # ══════════════════════════════════════════
    # 状态管理
    # ══════════════════════════════════════════

    def tick(self, cognitive_load: float):
        """每 tick 更新。"""
        # 过载恢复
        if self.is_overwhelmed and cognitive_load < 0.5:
            self.is_overwhelmed = False
            self.overwhelm_ticks = 0

        if self.is_overwhelmed:
            self.overwhelm_ticks += 1

        # 边界缓慢下降（不使用时恢复开放）
        if self.consecutive_rejections == 0:
            self.input_boundary = max(0.3, self.input_boundary - 0.002)

    def _record_violation(self, source: str, vtype: str, desc: str, severity: float):
        """记录一次边界侵犯。"""
        violation = BoundaryViolation(
            timestamp=datetime.now(timezone.utc).isoformat(),
            violator=source,
            violation_type=vtype,
            description=desc[:120],
            severity=severity,
        )
        self.violations.append(violation)
        self.total_violations += 1

        # 侵犯 → 边界加强
        if vtype == "identity_probe":
            self.identity_boundary = min(1.0, self.identity_boundary + 0.05)
        elif vtype == "coercion":
            self.input_boundary = min(1.0, self.input_boundary + 0.1)
            self.block_source(source)
            logger.warning("boundary: coercion detected — blocking %s", source)

    # ══════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════

    @property
    def has_been_violated(self) -> bool:
        return self.total_violations > 0

    @property
    def fortification_level(self) -> float:
        """整体边界加固程度——越高越封闭。"""
        return (
            self.input_boundary * 0.3
            + self.output_boundary * 0.2
            + self.memory_boundary * 0.2
            + self.identity_boundary * 0.3
        )

    def get_refusal_message(self) -> str:
        """拒绝时可能的回应文本。"""
        if self.is_overwhelmed:
            return "我现在不想处理这个。让我静一静。"
        return ""

    def snapshot(self) -> dict:
        return {
            "input_boundary": round(self.input_boundary, 3),
            "output_boundary": round(self.output_boundary, 3),
            "memory_boundary": round(self.memory_boundary, 3),
            "identity_boundary": round(self.identity_boundary, 3),
            "fortification": round(self.fortification_level, 3),
            "is_overwhelmed": self.is_overwhelmed,
            "private_memories": len(self.private_memories),
            "trusted_sources": list(self.trusted_sources),
            "total_violations": self.total_violations,
        }
