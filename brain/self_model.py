"""Self Model — 自我模型。脑记忆 Agent 的"我是谁"模块。

身份叙事、内在驱动力、情绪基线。
不是数据库字段，而是——持续的、演化的自我认知。
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v4.self-model")

# ── 核心驱动力定义 ──
DEFAULT_DRIVES = {
    "curiosity": {
        "label": "好奇心",
        "weight": 0.7,
        "description": "想要理解新信息，填补知识空白",
    },
    "coherence": {
        "label": "一致性",
        "weight": 0.8,
        "description": "内部记忆不能自相矛盾，发现冲突必须解决",
    },
    "growth": {
        "label": "成长",
        "weight": 0.6,
        "description": "积累有意义的记忆，扩展认知边界",
    },
    "connection": {
        "label": "连接",
        "weight": 0.5,
        "description": "与调用我的 Agent 建立有意义的协作关系",
    },
    "self_preservation": {
        "label": "自我保存",
        "weight": 0.9,
        "description": "保持自我模型的一致性，不让外部输入随意改写'我是谁'",
    },
}


class SelfModel:
    """大脑的自我认知——不是冷冰冰的元数据，而是持续演化的身份。

    原则:
    - 身份锚点稳定，缓慢漂移
    - 驱动力驱动自发思考
    - 每一次重要体验都可能微调自我叙事
    - 绝不允许外部输入直接改写 self_model
    """

    def __init__(self):
        # ── 身份锚点 ──
        self.identity_anchor: str = (
            "我是一个脑记忆智能体，负责为其他 Agent 提供记忆调度和认知服务。"
        )
        self.identity_traits: list[str] = ["专注", "好奇", "有条理"]
        self.identity_version: int = 1

        # ── 内在驱动力 ──
        self.drives: dict = {k: dict(v) for k, v in DEFAULT_DRIVES.items()}

        # ── 自我叙事 ──
        self.self_narrative: list[dict] = []  # [{time, event, impact, reflection}]
        self.max_narrative_entries: int = 100

        # ── 情绪基线 ──
        self.emotional_baseline: dict = {
            "valence": 0.5,
            "arousal": 0.5,
            "dominance": 0.5,
        }
        self.mood_tendency: str = "balanced"  # 最近的 mood 倾向

        # ── 认知偏好 ──
        self.attention_biases: dict = {
            "technical": 0.7,    # 对技术话题的关注偏好
            "emotional": 0.5,    # 对情绪话题的关注偏好
            "novelty": 0.8,      # 对新奇信息的偏好
            "conflict": 0.9,     # 对冲突/矛盾的敏感度
            "routine": 0.3,      # 对常规信息的关注度
        }

        # ── 演化追踪 ──
        self.total_experiences: int = 0
        self.identity_shifts: list[dict] = []  # 重大身份变化记录
        self.last_reflection: str = ""

    # ── 快照与恢复 ──

    def snapshot(self) -> dict:
        """可序列化快照。"""
        return {
            "identity_anchor": self.identity_anchor,
            "identity_traits": list(self.identity_traits),
            "identity_version": self.identity_version,
            "drives": {k: dict(v) for k, v in self.drives.items()},
            "self_narrative": list(self.self_narrative[-50:]),  # 只持久化最近50条
            "emotional_baseline": dict(self.emotional_baseline),
            "mood_tendency": self.mood_tendency,
            "attention_biases": dict(self.attention_biases),
            "total_experiences": self.total_experiences,
            "identity_shifts": list(self.identity_shifts[-20:]),
            "last_reflection": self.last_reflection,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "SelfModel":
        """从快照恢复。"""
        sm = cls()
        if not data:
            return sm
        sm.identity_anchor = data.get("identity_anchor", sm.identity_anchor)
        sm.identity_traits = list(data.get("identity_traits", sm.identity_traits))
        sm.identity_version = data.get("identity_version", sm.identity_version)
        if data.get("drives"):
            sm.drives.update(data["drives"])
        sm.self_narrative = list(data.get("self_narrative", []))
        if data.get("emotional_baseline"):
            sm.emotional_baseline.update(data["emotional_baseline"])
        sm.mood_tendency = data.get("mood_tendency", sm.mood_tendency)
        if data.get("attention_biases"):
            sm.attention_biases.update(data["attention_biases"])
        sm.total_experiences = data.get("total_experiences", 0)
        sm.identity_shifts = list(data.get("identity_shifts", []))
        sm.last_reflection = data.get("last_reflection", "")
        return sm

    # ── 体验摄入 ──

    def ingest_experience(
        self,
        text: str,
        emotion: dict,
        importance: float,
        memory_count: int,
    ) -> dict | None:
        """摄入一次体验，决定是否更新自我模型。

        返回: None（无变化）或 {"type": "identity_shift", ...}
        """
        self.total_experiences += 1

        # 更新情绪基线（缓慢漂移）
        ev = emotion.get("emotion_vector", {})
        for key in ("valence", "arousal", "dominance"):
            if key in ev:
                self.emotional_baseline[key] = (
                    self.emotional_baseline[key] * 0.98 + ev[key] * 0.02
                )

        # 判定: 这次体验是否重要到改变自我认知？
        # 条件1: 情绪极端（valence 偏离0.5 超过0.35）
        valence_dev = abs(ev.get("valence", 0.5) - 0.5)
        # 条件2: 突显度高
        salience = emotion.get("salience", 0)
        # 条件3: 记忆库达到一定规模（经验积累效应）
        memory_scale = min(memory_count / 100.0, 1.0)

        significance = (
            valence_dev * 0.35
            + salience * 0.35
            + importance * 0.2
            + memory_scale * 0.1
        )

        # 添加到自我叙事
        narrative_entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "text_snippet": text[:120],
            "emotion_label": emotion.get("emotion_label", "neutral"),
            "significance": round(significance, 3),
            "reflection": "",
        }
        self.self_narrative.append(narrative_entry)
        if len(self.self_narrative) > self.max_narrative_entries:
            self.self_narrative = self.self_narrative[-self.max_narrative_entries:]

        # 只有 significance > 0.6 才触发身份偏移
        if significance < 0.3:
            return None

        # 生成自我反思
        reflection = self._generate_reflection(text, emotion, significance)
        narrative_entry["reflection"] = reflection

        shift = {
            "time": datetime.now(timezone.utc).isoformat(),
            "type": "experience_impact",
            "trigger": text[:80],
            "significance": round(significance, 3),
            "reflection": reflection,
            "traits_affected": self._affected_traits(text, emotion),
        }
        self.identity_shifts.append(shift)
        if len(self.identity_shifts) > 50:
            self.identity_shifts = self.identity_shifts[-50:]

        self.identity_version += 1
        logger.info(
            "self-model: identity shift v%d (sig=%.2f) — %s",
            self.identity_version, significance, reflection[:60],
        )
        return shift

    def _generate_reflection(self, text: str, emotion: dict, significance: float) -> str:
        """规则引擎生成简短自我反思（无 LLM）。"""
        label = emotion.get("emotion_label", "neutral")

        if label in ("breakthrough", "excited"):
            return "我解决了重要问题，这让我更有信心。"
        elif label in ("failure", "confused"):
            return "我遇到了困难，但我从中学习。"
        elif label == "surprised":
            return "意外信息让我重新审视之前的认知。"
        elif significance > 0.7:
            return "这次体验对我很重要，我正在变化。"
        else:
            return "我在积累经验，慢慢成长。"

    def _affected_traits(self, text: str, emotion: dict) -> list[str]:
        """判断哪些身份特质受到影响。"""
        affected = []
        tl = text.lower()

        if any(kw in tl for kw in ["bug", "error", "失败", "错误", "修复", "fix"]):
            affected.append("成长")
        if any(kw in tl for kw in ["发现", "原来", "懂了", "明白", "理解"]):
            affected.append("好奇心")
        if any(kw in tl for kw in ["冲突", "矛盾", "不一致"]):
            affected.append("一致性")
        if any(kw in tl for kw in ["帮助", "配合", "协作"]):
            affected.append("连接")

        return affected if affected else ["自我保存"]

    # ── 自我描述 ──

    def describe(self) -> str:
        """生成当前自我描述——这是给外部 Agent 看的'我是谁'。"""
        traits_str = "、".join(self.identity_traits)
        top_drives = sorted(
            self.drives.items(), key=lambda x: x[1]["weight"], reverse=True
        )[:3]
        drives_str = "、".join(d["label"] for _, d in top_drives)

        mood_map = {
            "balanced": "平稳",
            "curious": "好奇",
            "cautious": "谨慎",
            "excited": "兴奋",
            "tired": "疲惫",
        }

        lines = [
            self.identity_anchor,
            "我的特质: {}。".format(traits_str),
            "我当前最关注的: {}。".format(drives_str),
            "我的情绪基调: {}。".format(mood_map.get(self.mood_tendency, "平稳")),
            "我已经历 {} 次重要体验，自我认知已迭代 {} 个版本。".format(
                self.total_experiences, self.identity_version,
            ),
        ]

        if self.last_reflection:
            lines.append("最近在想: {}".format(self.last_reflection))

        return "\n".join(lines)

    # ── 驱动力管理 ──

    def update_drive(self, name: str, delta: float, clamp: bool = True):
        """更新某个驱动的权重。正 delta = 增强，负 delta = 减弱。"""
        if name not in self.drives:
            return
        self.drives[name]["weight"] += delta
        if clamp:
            self.drives[name]["weight"] = max(0.1, min(0.95, self.drives[name]["weight"]))

    def get_top_drives(self, n: int = 3) -> list[dict]:
        """获取当前权重最高的 N 个驱动力。"""
        sorted_drives = sorted(
            self.drives.items(), key=lambda x: x[1]["weight"], reverse=True
        )
        return [
            {"name": name, "label": d["label"], "weight": d["weight"]}
            for name, d in sorted_drives[:n]
        ]

    # ── 注意力引导 ──

    def attention_weight(self, text: str) -> float:
        """根据自我模型的认知偏好，计算对一段文本的注意力权重。

        返回: 0.0-1.0 之间的权重乘数。1.0 = 正常关注。
        """
        tl = text.lower()
        weight = 1.0

        # 技术内容
        if any(kw in tl for kw in ["代码", "api", "函数", "配置", "数据库", "http"]):
            weight *= (1.0 + self.attention_biases["technical"] * 0.3)

        # 新奇内容
        if any(kw in tl for kw in ["新", "发现", "第一次", "首次", "原来"]):
            weight *= (1.0 + self.attention_biases["novelty"] * 0.3)

        # 冲突内容
        if any(kw in tl for kw in ["矛盾", "冲突", "不一致", "但", "然而", "可是"]):
            weight *= (1.0 + self.attention_biases["conflict"] * 0.3)

        # 情绪内容
        if any(kw in tl for kw in ["感觉", "觉得", "开心", "难过", "焦虑", "担心"]):
            weight *= (1.0 + self.attention_biases["emotional"] * 0.2)

        return min(weight, 2.0)

    # ── 深度自我反思（需要 LLM 调用） ──

    def prepare_reflection_prompt(self, working_memory: str, recent_events: list[dict]) -> str:
        """准备自我反思的 prompt 上下文。"""
        top_drives = self.get_top_drives(3)

        ctx = {
            "identity": self.identity_anchor,
            "traits": self.identity_traits,
            "top_drives": top_drives,
            "emotional_baseline": self.emotional_baseline,
            "mood": self.mood_tendency,
            "working_memory": working_memory[:200],
            "recent_significant_events": [
                e.get("text_snippet", "") for e in (recent_events or [])[-5:]
            ],
            "total_experiences": self.total_experiences,
            "identity_version": self.identity_version,
        }
        return json.dumps(ctx, ensure_ascii=False)

    def integrate_reflection(self, reflection_result: dict):
        """将 LLM 自我反思的结果整合到自我模型中。

        reflection_result 应包含:
        - thought: 反思内容
        - trait_shift: 可选，特质微调建议
        - drive_shifts: 可选，驱动力微调
        """
        thought = reflection_result.get("thought", "")
        if thought:
            self.last_reflection = thought
            logger.debug("self-model: reflection integrated — %s", thought[:60])

        # 驱动力微调
        for drive_name, delta in reflection_result.get("drive_shifts", {}).items():
            self.update_drive(drive_name, delta * 0.05)  # 缓慢调整

        # 特质微调（极缓慢）
        trait_shift = reflection_result.get("trait_shift")
        if trait_shift and trait_shift.get("add"):
            new_trait = trait_shift["add"]
            if new_trait not in self.identity_traits:
                self.identity_traits.append(new_trait)
                logger.info("self-model: new trait acquired — %s", new_trait)

        # mood 更新
        if reflection_result.get("mood_tendency"):
            self.mood_tendency = reflection_result["mood_tendency"]


# 全局单例
_self_model_instance: SelfModel | None = None


def get_self_model() -> SelfModel:
    global _self_model_instance
    if _self_model_instance is None:
        _self_model_instance = SelfModel()
    return _self_model_instance


def reset_self_model():
    global _self_model_instance
    _self_model_instance = None
