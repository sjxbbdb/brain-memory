"""Procedural Memory — v5.4 程序记忆。从重复成功中学习"怎么做"。

v5.0 的 basal_ganglia 是硬编码的关键词匹配——4 种预置习惯，不会成长。
v5.1 的目标引擎让你"想做"，v5.2 的元认知让你审视"想得对不对"，
v5.4 的程序记忆让你"越做越好"。

核心机制:
  - 技能模板：从意图序列（intent → tool_call → 结果）中自动提取成功模式
  - 强化学习：成功→置信度上升，失败→置信度下降
  - 技能组合：发现 A→B→C 的链式模式
  - 情境匹配：当前上下文匹配最适用的技能
  - 遗忘曲线：长期不用的技能缓慢衰减

设计原则:
  - 从经验中学习，不需要预定义
  - 轻量：规则引擎 + 模式匹配，不新增 LLM 调用
  - 技能是"建议"不是"指令"——大脑仍然自主决策
"""

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v5.procedural-memory")


@dataclass
class SkillStep:
    """技能中的一个步骤。"""
    action_type: str   # intent 类型: call_tool/respond/think
    tool_name: str     # 工具名（如果是 call_tool）
    description: str   # 自然语言描述
    precondition: str  # 前置条件描述
    expected_result: str  # 预期结果特征


@dataclass
class SkillTemplate:
    """从经验中提取的可复用技能。"""
    id: str
    name: str                  # 技能名称
    description: str           # 做什么
    steps: list[SkillStep]     # 步骤序列
    preconditions: list[str]   # ["需要网络", "需要文件路径"]
    postconditions: list[str]  # ["搜索完成", "文件已读取"]
    success_count: int = 0     # 成功次数
    failure_count: int = 0     # 失败次数
    confidence: float = 0.3    # 0-1，当前可靠度
    last_used: str = ""        # ISO timestamp
    created_at: str = ""
    decay_rate: float = 0.02   # 每次衰减降低的 confidence

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def reliability(self) -> float:
        """成功率。"""
        total = self.success_count + self.failure_count
        if total == 0:
            return self.confidence
        return self.success_count / total

    @property
    def is_mastered(self) -> bool:
        """是否已掌握（confidence > 0.8 且至少成功 3 次）。"""
        return self.confidence > 0.8 and self.success_count >= 3

    @property
    def is_forgotten(self) -> bool:
        """是否已遗忘（confidence < 0.1）。"""
        return self.confidence < 0.1


class ProceduralMemory:
    """程序记忆引擎——从经验中学习可复用的操作模式。

    不是预定义的"习惯"，而是从成功经验中提取的"技能"。
    技能可以强化（成功）、削弱（失败）、遗忘（长期不用）。
    """

    def __init__(self):
        self.skills: dict[str, SkillTemplate] = {}  # id → SkillTemplate
        self._skill_index: dict[str, list[str]] = {}  # tool_name → [skill_ids]

        # 经验缓冲区：积累到一定量后触发模式提取
        self._experience_buffer: deque = deque(maxlen=20)  # [{intent, tool, success, context}]
        self._extraction_threshold: int = 5  # 积累 N 条经验后提取一次

        # 统计
        self.total_skills_learned: int = 0
        self.total_skills_mastered: int = 0
        self.total_extractions: int = 0

    # ══════════════════════════════════════════════
    # 经验摄入
    # ══════════════════════════════════════════════

    def record_experience(
        self,
        intent_type: str,
        tool_name: str,
        success: bool,
        context: str = "",
        result_summary: str = "",
    ):
        """记录一次操作经验。"""
        self._experience_buffer.append({
            "intent_type": intent_type,
            "tool_name": tool_name,
            "success": success,
            "context": context[:200],
            "result": result_summary[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # 触发模式提取
        if len(self._experience_buffer) >= self._extraction_threshold:
            self._extract_patterns()

    # ══════════════════════════════════════════════
    # 技能匹配
    # ══════════════════════════════════════════════

    def match_skill(self, context: str, goal: str = "") -> SkillTemplate | None:
        """根据当前情境匹配最适用的技能。

        返回: 最佳匹配的技能，或 None（没有适用的技能）
        """
        best_skill = None
        best_score = 0.0

        for skill in self.skills.values():
            if skill.is_forgotten:
                continue

            score = self._match_score(skill, context, goal)
            if score > best_score:
                best_score = score
                best_skill = skill

        # 阈值：得分太低不推荐
        if best_score < 0.3:
            return None

        return best_skill

    def _match_score(self, skill: SkillTemplate, context: str, goal: str) -> float:
        """计算技能与当前情境的匹配度。"""
        score = skill.confidence * 0.4  # 基础分：技能可靠度

        # 前置条件匹配
        ctx_lower = context.lower() + " " + goal.lower()
        if skill.preconditions:
            matched_pre = sum(
                1 for pre in skill.preconditions
                if any(word in ctx_lower for word in pre.lower().split())
            )
            score += (matched_pre / max(1, len(skill.preconditions))) * 0.3

        # 描述匹配
        desc_words = set(skill.description.lower().split())
        ctx_words = set(ctx_lower.split())
        overlap = len(desc_words & ctx_words)
        if overlap > 0:
            score += min(0.3, overlap * 0.05)

        # 近期使用加成
        if skill.last_used:
            try:
                last = datetime.fromisoformat(skill.last_used.replace("Z", "+00:00"))
                hours_ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if hours_ago < 1:
                    score += 0.15
                elif hours_ago < 6:
                    score += 0.08
            except (ValueError, TypeError):
                pass

        return min(1.0, score)

    # ══════════════════════════════════════════════
    # 技能反馈
    # ══════════════════════════════════════════════

    def reinforce_skill(self, skill_id: str, success: bool):
        """强化或削弱技能。"""
        skill = self.skills.get(skill_id)
        if not skill:
            return

        if success:
            skill.success_count += 1
            skill.confidence = min(1.0, skill.confidence * 1.15 + 0.05)
        else:
            skill.failure_count += 1
            skill.confidence = max(0.05, skill.confidence * 0.85)

        skill.last_used = datetime.now(timezone.utc).isoformat()

        if skill.is_mastered and skill.success_count == 3:
            self.total_skills_mastered += 1
            logger.info("procedural: skill mastered — %s (confidence=%.2f)", skill.name, skill.confidence)

    # ══════════════════════════════════════════════
    # 技能列表
    # ══════════════════════════════════════════════

    def get_available_skills(self) -> list[SkillTemplate]:
        """获取所有可用技能（未遗忘、按置信度排序）。"""
        available = [s for s in self.skills.values() if not s.is_forgotten]
        return sorted(available, key=lambda s: s.confidence, reverse=True)

    def get_mastered_skills(self) -> list[SkillTemplate]:
        return [s for s in self.skills.values() if s.is_mastered]

    def get_skill_suggestion(self, context: str) -> str | None:
        """获取技能建议（自然语言）。"""
        skill = self.match_skill(context)
        if skill:
            return f"我之前做过类似的事: {skill.description}（成功率 {skill.reliability:.0%}）"
        return None

    # ══════════════════════════════════════════════
    # 衰减
    # ══════════════════════════════════════════════

    def decay_skills(self):
        """所有技能缓慢衰减——长期不用的技能会遗忘。"""
        for skill in list(self.skills.values()):
            if skill.last_used:
                try:
                    last = datetime.fromisoformat(skill.last_used.replace("Z", "+00:00"))
                    days_since = (datetime.now(timezone.utc) - last).total_seconds() / 86400
                    decay = skill.decay_rate * days_since * 0.5
                    skill.confidence = max(0.02, skill.confidence - decay)
                except (ValueError, TypeError):
                    pass

            # 清除已遗忘的技能
            if skill.is_forgotten and skill.failure_count > 5:
                del self.skills[skill.id]
                # 清理索引
                for idx in self._skill_index.values():
                    if skill.id in idx:
                        idx.remove(skill.id)

    # ══════════════════════════════════════════════
    # 内部：模式提取
    # ══════════════════════════════════════════════

    def _extract_patterns(self):
        """从经验缓冲区中提取可复用的操作模式。"""
        recent = list(self._experience_buffer)[-self._extraction_threshold:]
        if len(recent) < 2:
            return

        # 策略1：连续成功序列 → 技能
        success_runs = self._find_success_runs(recent)
        for run in success_runs:
            if len(run) >= 2:
                self._create_skill_from_run(run)

        # 策略2：相同的 tool_name 出现 3+ 次且多数成功 → 技能
        tool_counts = {}
        for exp in recent:
            tn = exp["tool_name"]
            if tn:
                if tn not in tool_counts:
                    tool_counts[tn] = {"success": 0, "total": 0}
                tool_counts[tn]["total"] += 1
                if exp["success"]:
                    tool_counts[tn]["success"] += 1

        for tool_name, stats in tool_counts.items():
            if stats["total"] >= 3 and stats["success"] / stats["total"] >= 0.66:
                # 检查是否已存在相似技能
                if not any(s.name.startswith(f"使用{tool_name}") for s in self.skills.values()):
                    skill = SkillTemplate(
                        id=f"skill-{uuid.uuid4().hex[:8]}",
                        name=f"使用{tool_name}获取信息",
                        description=f"调用 {tool_name} 获取需要的信息",
                        steps=[SkillStep(
                            action_type="call_tool",
                            tool_name=tool_name,
                            description=f"执行 {tool_name}",
                            precondition="有明确的信息需求",
                            expected_result="获得相关信息或错误提示",
                        )],
                        preconditions=[f"需要查询或搜索"],
                        postconditions=["获得信息结果"],
                        confidence=0.4,
                    )
                    self._add_skill(skill)
                    logger.info("procedural: new skill learned — %s", skill.name)

        self.total_extractions += 1

    def _find_success_runs(self, experiences: list[dict]) -> list[list[dict]]:
        """找出连续的成功序列。"""
        runs = []
        current_run = []
        for exp in experiences:
            if exp["success"]:
                current_run.append(exp)
            else:
                if len(current_run) >= 2:
                    runs.append(current_run)
                current_run = []
        if len(current_run) >= 2:
            runs.append(current_run)
        return runs

    def _create_skill_from_run(self, run: list[dict]):
        """从成功序列创建技能模板。"""
        # 去重：检查是否已存在类似序列
        run_pattern = tuple(e["tool_name"] for e in run)
        for skill in self.skills.values():
            skill_pattern = tuple(s.tool_name for s in skill.steps)
            if skill_pattern == run_pattern:
                skill.success_count += 1
                skill.confidence = min(1.0, skill.confidence + 0.05)
                skill.last_used = datetime.now(timezone.utc).isoformat()
                return

        # 创建新技能
        steps = []
        for exp in run:
            steps.append(SkillStep(
                action_type=exp["intent_type"],
                tool_name=exp["tool_name"],
                description=f"执行 {exp['tool_name']}",
                precondition="",
                expected_result="成功",
            ))

        tool_names = " → ".join(e["tool_name"] for e in run if e["tool_name"])
        skill = SkillTemplate(
            id=f"skill-{uuid.uuid4().hex[:8]}",
            name=f"链式操作: {tool_names}",
            description=f"依次执行 {tool_names} 来完成一个复合任务",
            steps=steps,
            preconditions=[],
            postconditions=["操作链完成"],
            confidence=0.35,
        )
        self._add_skill(skill)
        self.total_skills_learned += 1
        logger.info("procedural: chain skill learned — %s", skill.name)

    def _add_skill(self, skill: SkillTemplate):
        """添加技能并更新索引。"""
        self.skills[skill.id] = skill
        for step in skill.steps:
            if step.tool_name:
                if step.tool_name not in self._skill_index:
                    self._skill_index[step.tool_name] = []
                self._skill_index[step.tool_name].append(skill.id)

    # ══════════════════════════════════════════════
    # 快照
    # ══════════════════════════════════════════════

    def snapshot(self) -> dict:
        skills_data = []
        for s in sorted(self.skills.values(), key=lambda x: x.confidence, reverse=True)[:10]:
            skills_data.append({
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "steps_count": len(s.steps),
                "confidence": round(s.confidence, 2),
                "reliability": round(s.reliability, 2),
                "successes": s.success_count,
                "failures": s.failure_count,
                "is_mastered": s.is_mastered,
                "tool_chain": " → ".join(st.tool_name for st in s.steps if st.tool_name),
            })

        return {
            "total_skills": len(self.skills),
            "available_skills": len(self.get_available_skills()),
            "mastered_skills": self.total_skills_mastered,
            "skills": skills_data,
            "experience_buffer_size": len(self._experience_buffer),
            "total_extractions": self.total_extractions,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "ProceduralMemory":
        pm = cls()
        if not data:
            return pm
        for sd in data.get("skills", []):
            skill = SkillTemplate(
                id=sd.get("id", ""),
                name=sd.get("name", ""),
                description=sd.get("description", ""),
                steps=[],
                preconditions=[],
                postconditions=[],
                success_count=sd.get("successes", 0),
                failure_count=sd.get("failures", 0),
                confidence=sd.get("confidence", 0.3),
            )
            pm.skills[skill.id] = skill
        pm.total_skills_learned = data.get("total_skills", 0)
        pm.total_skills_mastered = data.get("mastered_skills", 0)
        pm.total_extractions = data.get("total_extractions", 0)
        return pm
