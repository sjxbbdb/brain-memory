"""Curiosity Engine — 内在动机引擎。大脑的"我想知道..."模块。

不是被动等待输入，而是主动生成好奇心和待解问题。
驱动力导向：好奇心权重高 → 更多探索性问题；一致性权重高 → 更多验证性问题。

原则:
- 闲置时自发产生问题（不是每 tick，而是累积到一定量才触发）
- 问题来自记忆库中的"空白"和"矛盾"
- 驱动力权重影响问题类型
- 问题可以随着新记忆的摄入而被"解答"
"""
import logging
import random
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v4.curiosity")

# ── 问题模板（按驱动力分类） ──
QUESTION_TEMPLATES = {
    "curiosity": [
        "关于{entity}，我还知道什么？",
        "{entity}和其他东西有什么关联？",
        "为什么{entity}会这样？",
        "有没有我遗漏的关于{entity}的信息？",
    ],
    "coherence": [
        "{entity_a}和{entity_b}之间有没有矛盾？",
        "我之前关于{entity}的认知是否需要修正？",
        "有没有证据支持{entity}这个结论？",
        "我的哪条记忆和{entity}冲突？",
    ],
    "growth": [
        "通过{entity}我能学到什么？",
        "{entity}这个经验如何应用到其他地方？",
        "我应该记住{entity}的什么教训？",
        "这件事对我有什么深远影响？",
    ],
    "connection": [
        "关于{entity}，调用我的 Agent 还需要什么？",
        "我能在{entity}方面提供什么帮助？",
        "其他 Agent 是否关心{entity}？",
    ],
    "self_preservation": [
        "{entity}会不会影响我的稳定性？",
        "我的自我认知在{entity}之后需要调整吗？",
        "这次体验有没有动摇我的核心判断？",
    ],
}


class CuriosityEngine:
    """内在动机引擎——大脑的"好奇心"。

    不完全依赖外部输入，而是有自己的问题列表和探索方向。
    """

    def __init__(self):
        # ── 待解问题 ──
        self.open_questions: list[dict] = []  # [{id, question, drive, created, resolved_at}]
        self.max_open_questions: int = 20

        # ── 已解问题 ──
        self.resolved_questions: list[dict] = []  # 最近解决的
        self.max_resolved: int = 50

        # ── 探索状态 ──
        self.exploration_topics: list[str] = []  # 当前在探索的主题
        self.spontaneous_thoughts: list[str] = []  # 最近的自发想法
        self.max_spontaneous: int = 20

        # ── 统计 ──
        self.total_questions_generated: int = 0
        self.total_questions_resolved: int = 0
        self.last_generation_time: float = 0.0

    # ── 问题生成 ──

    def generate_questions(
        self,
        top_drives: list[dict],
        recent_entities: list[str],
        memory_gaps: list[str] | None = None,
    ) -> list[dict]:
        """根据当前驱动力和记忆上下文生成新问题。

        Args:
            top_drives: 当前权重最高的驱动力 [{name, label, weight}]
            recent_entities: 最近记忆中的实体/关键词
            memory_gaps: 可能的记忆空白（可选）

        Returns:
            [{id, question, drive, created}]
        """
        if not recent_entities and not memory_gaps:
            return []

        new_questions = []
        entities = recent_entities[:8]  # 取前8个实体

        # 根据驱动力权重分配问题配额
        for drive in top_drives[:3]:
            templates = QUESTION_TEMPLATES.get(drive["name"], [])
            if not templates:
                continue

            # 权重越高，生成的问题越多
            quota = max(1, int(drive["weight"] * 3))
            for _ in range(min(quota, 2)):
                if not entities:
                    break
                template = random.choice(templates)
                entity = random.choice(entities)

                # 填充模板
                question = template.replace("{entity}", entity)
                # 处理双实体模板
                if "{entity_a}" in question and len(entities) >= 2:
                    a, b = random.sample(entities, 2)
                    question = question.replace("{entity_a}", a).replace("{entity_b}", b)
                elif "{entity_a}" in question:
                    question = question.replace("{entity_a}", entity).replace("{entity_b}", "相关概念")

                # 去重：检查是否已存在相同问题
                if any(q["question"] == question for q in self.open_questions):
                    continue

                q = {
                    "id": "q-{0}".format(len(self.open_questions) + len(new_questions) + 1),
                    "question": question,
                    "drive": drive["name"],
                    "drive_label": drive["label"],
                    "created": datetime.now(timezone.utc).isoformat(),
                    "resolved_at": None,
                }
                new_questions.append(q)

        # 如果有记忆空白，生成针对性的问题
        if memory_gaps:
            for gap in memory_gaps[:3]:
                q = {
                    "id": "q-gap-{0}".format(len(self.open_questions) + len(new_questions) + 1),
                    "question": "我需要更多关于{0}的信息".format(gap[:50]),
                    "drive": "curiosity",
                    "drive_label": "好奇心",
                    "created": datetime.now(timezone.utc).isoformat(),
                    "resolved_at": None,
                }
                new_questions.append(q)

        # 添加到待解列表
        for q in new_questions:
            if len(self.open_questions) >= self.max_open_questions:
                # 移除最旧的问题
                self.open_questions.pop(0)
            self.open_questions.append(q)

        self.total_questions_generated += len(new_questions)
        self.last_generation_time = datetime.now(timezone.utc).timestamp()

        if new_questions:
            logger.info(
                "curiosity: %d new questions generated (total open: %d)",
                len(new_questions), len(self.open_questions),
            )

        return new_questions

    # ── 问题解答检测 ──

    def check_resolution(self, new_text: str, new_entities: list[str]) -> list[dict]:
        """检查新摄入的信息是否解答了任何待解问题。

        返回: 已解答的问题列表
        """
        resolved = []
        tl = new_text.lower()
        remaining = []

        for q in self.open_questions:
            question_lower = q["question"].lower()
            # 简单规则：如果新文本和问题有关键词重叠
            q_words = set(question_lower.replace("？", "").replace("?", "").split())
            t_words = set(tl.split())

            # 如果问题中的实体出现在新文本中
            overlap = q_words & t_words
            entity_match = any(e.lower() in tl for e in new_entities)

            if len(overlap) >= 2 or (len(overlap) >= 1 and entity_match):
                q["resolved_at"] = datetime.now(timezone.utc).isoformat()
                q["resolved_by"] = new_text[:200]
                resolved.append(q)
                self.resolved_questions.append(q)
                if len(self.resolved_questions) > self.max_resolved:
                    self.resolved_questions = self.resolved_questions[-self.max_resolved:]
            else:
                remaining.append(q)

        self.open_questions = remaining
        self.total_questions_resolved += len(resolved)

        if resolved:
            logger.info("curiosity: %d questions resolved!", len(resolved))

        return resolved

    # ── 自发思考 ──

    def spontaneous_think(self, working_memory: str, top_drives: list[dict]) -> str | None:
        """在闲置时产生自发想法。

        返回: 自发想法字符串，或 None（没什么可想的）
        """
        # 优先从待解问题中选一个
        if self.open_questions and random.random() < 0.6:
            q = random.choice(self.open_questions)
            thought = "[{0}] {1}".format(q["drive_label"], q["question"])
            self.spontaneous_thoughts.append(thought)
            if len(self.spontaneous_thoughts) > self.max_spontaneous:
                self.spontaneous_thoughts = self.spontaneous_thoughts[-self.max_spontaneous:]
            return thought

        # 否则，基于驱动力产生模糊的想法
        if not top_drives:
            return None

        drive_names = [d["name"] for d in top_drives[:2]]
        thoughts = []

        if "curiosity" in drive_names:
            thoughts.append("我在想...还有什么是我不知道的？")
        if "coherence" in drive_names:
            thoughts.append("最近的信息之间有没有矛盾？")
        if "growth" in drive_names:
            thoughts.append("我从最近的经历中学到了什么？")
        if "connection" in drive_names:
            thoughts.append("调用我的 Agent 现在需要什么帮助？")
        if "self_preservation" in drive_names:
            thoughts.append("我还在保持自我吗？")

        if not thoughts:
            return None

        thought = random.choice(thoughts) if thoughts else None
        if thought:
            self.spontaneous_thoughts.append(thought)
            if len(self.spontaneous_thoughts) > self.max_spontaneous:
                self.spontaneous_thoughts = self.spontaneous_thoughts[-self.max_spontaneous:]
        return thought

    # ── 探索主题 ──

    def update_exploration_topics(self, entities: list[str]):
        """根据最近的实体更新探索主题。"""
        for entity in entities[:5]:
            if entity not in self.exploration_topics:
                self.exploration_topics.append(entity)
        if len(self.exploration_topics) > 10:
            self.exploration_topics = self.exploration_topics[-10:]

    # ── 快照 ──

    def snapshot(self) -> dict:
        return {
            "open_questions": self.open_questions,
            "resolved_questions": self.resolved_questions[-20:],
            "exploration_topics": self.exploration_topics,
            "spontaneous_thoughts": self.spontaneous_thoughts[-10:],
            "total_generated": self.total_questions_generated,
            "total_resolved": self.total_questions_resolved,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "CuriosityEngine":
        ce = cls()
        if not data:
            return ce
        ce.open_questions = data.get("open_questions", [])
        ce.resolved_questions = data.get("resolved_questions", [])
        ce.exploration_topics = data.get("exploration_topics", [])
        ce.spontaneous_thoughts = data.get("spontaneous_thoughts", [])
        ce.total_questions_generated = data.get("total_generated", 0)
        ce.total_questions_resolved = data.get("total_resolved", 0)
        return ce

    @property
    def pending_count(self) -> int:
        return len(self.open_questions)


# 全局单例
_curiosity_instance: CuriosityEngine | None = None


def get_curiosity() -> CuriosityEngine:
    global _curiosity_instance
    if _curiosity_instance is None:
        _curiosity_instance = CuriosityEngine()
    return _curiosity_instance
