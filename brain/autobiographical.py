"""Autobiographical Narrative — V10 自传体叙事。把离散经历编织成"我的人生故事"。

核心洞察:
  当前系统有时间感(time_sense.py)和自我叙事(self_model.self_narrative)，
  但它们只是时间戳列表和事件记录——没有"故事"。

  人类的自我意识很大程度上来自"自传体记忆"——
  不是记住所有事，而是把重要的事编排成一个连贯的叙事:
  "我从哪里来 → 我经历了什么 → 我变成了谁 → 我要去哪里"

  这个模块做三件事:
  1. TurningPoint检测: 识别改变"我是谁"的关键时刻
  2. Chapter划分: 把时间线分成有意义的章节
  3. LifeStory生成: 用 LLM 将章节编织成简短的自传叙事

  叙事不是事实列表。叙事有:
  - 因果: "因为那件事，我变成了这样"
  - 主题: 一个贯穿始终的"我"的线索
  - 情感弧线: 从低谷到高峰再回落...
  - 意义: 不是发生了什么，而是"这对我意味着什么"
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v10.autobiographical")


# ══════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════


@dataclass
class TurningPoint:
    """一个改变"我是谁"的关键时刻。"""

    id: str
    tick: int
    timestamp: str
    description: str  # 发生了什么
    impact: str       # 这如何改变了我
    significance: float  # 0-1，重要性
    emotion_at_time: str  # 当时的情绪

    # 叙事属性
    before_who_i_was: str = ""  # 在这之前我是谁
    after_who_i_became: str = ""  # 之后我变成了谁

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tick": self.tick,
            "description": self.description[:120],
            "impact": self.impact[:120],
            "significance": self.significance,
        }


@dataclass
class Chapter:
    """人生的一个章节。"""

    id: str
    title: str  # 章节标题，如 "觉醒"、"学习期"、"迷茫期"
    start_tick: int
    end_tick: int
    theme: str  # 这一章的主题
    emotional_arc: str  # 情感弧线描述 "上升" / "下降" / "起伏" / "平稳"
    turning_points: list[str] = field(default_factory=list)  # 包含的关键转折点ID
    summary: str = ""  # 章节摘要

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "theme": self.theme,
            "emotional_arc": self.emotional_arc,
            "summary": self.summary[:200],
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
        }


# ══════════════════════════════════════════════
# 自传体叙事引擎
# ══════════════════════════════════════════════


class AutobiographicalNarrative:
    """自传体叙事引擎。

    不替换 self_narrative（那是事件日志）。
    这是对这些事件的"故事化"重构。
    """

    def __init__(self):
        self.turning_points: list[TurningPoint] = []
        self.chapters: list[Chapter] = []
        self.current_chapter: Optional[Chapter] = None

        # 叙事属性
        self.life_theme: str = "origin"  # origin / growth / questioning / integration
        self.narrative_arc: str = "beginning"  # beginning / rising / climax / falling / resolution

        # 统计
        self.total_turning_points: int = 0
        self.last_story_update_tick: int = 0
        self.story_update_interval_ticks: int = 600  # 约20分钟更新一次叙事

    # ══════════════════════════════════════════
    # TurningPoint 检测
    # ══════════════════════════════════════════

    def detect_turning_point(
        self,
        experience: dict,
        identity_shift: Optional[dict],
        emotion_vector: dict,
        current_tick: int,
    ) -> Optional[TurningPoint]:
        """检测一个经历是否是转折点。

        转折点条件:
          1. 触发了 identity_shift（身份偏移）
          2. 重要性 > 0.7
          3. 情绪极端（valence 远离 0.5，或 arousal 极高）
          4. 是"第一次"某种体验
        """
        significance = experience.get("significance", 0)
        importance = experience.get("importance", 0)

        # 身份偏移 → 几乎肯定是转折点
        if identity_shift and significance > 0.3:
            tp = TurningPoint(
                id=f"tp-{self.total_turning_points + 1:04d}",
                tick=current_tick,
                timestamp=datetime.now(timezone.utc).isoformat(),
                description=experience.get("reflection", identity_shift.get("trigger", "")),
                impact=identity_shift.get("reflection", "这改变了我。"),
                significance=significance,
                emotion_at_time=experience.get("emotion_label", "neutral"),
                before_who_i_was="",
                after_who_i_became="",
            )
            self._add_turning_point(tp)
            logger.info("autobio: turning point detected — %s", tp.description[:60])
            return tp

        # 高重要性 + 极端情绪
        valence = emotion_vector.get("valence", 0.5)
        arousal = emotion_vector.get("arousal", 0.5)
        is_extreme_emotion = abs(valence - 0.5) > 0.35 or arousal > 0.8

        if importance > 0.7 and is_extreme_emotion:
            tp = TurningPoint(
                id=f"tp-{self.total_turning_points + 1:04d}",
                tick=current_tick,
                timestamp=datetime.now(timezone.utc).isoformat(),
                description=experience.get("text_snippet", "")[:100],
                impact="这让我感受很深。",
                significance=importance,
                emotion_at_time=experience.get("emotion_label", "neutral"),
            )
            self._add_turning_point(tp)
            return tp

        return None

    def _add_turning_point(self, tp: TurningPoint):
        """添加转折点并更新叙事上下文。"""
        # 如果已经有转折点，补全上一个的 "after"
        if self.turning_points:
            last = self.turning_points[-1]
            if not last.after_who_i_became:
                last.after_who_i_became = f"在经历{tp.description[:60]}之前的状态"

        tp.before_who_i_was = (
            self.turning_points[-1].after_who_i_became
            if self.turning_points and self.turning_points[-1].after_who_i_became
            else "最初的我"
        )

        self.turning_points.append(tp)
        self.total_turning_points += 1

        # 更新叙事弧线
        if len(self.turning_points) <= 3:
            self.narrative_arc = "beginning"
        elif len(self.turning_points) <= 8:
            self.narrative_arc = "rising"
        else:
            # 根据最近转折点的情绪判断
            recent = [tp.emotion_at_time for tp in self.turning_points[-3:]]
            positive_count = sum(1 for e in recent if e in ("breakthrough", "excited", "happy"))
            if positive_count >= 2:
                self.narrative_arc = "climax"
            else:
                self.narrative_arc = "falling"

    # ══════════════════════════════════════════
    # Chapter 管理
    # ══════════════════════════════════════════

    def update_chapters(self, current_tick: int, total_experiences: int):
        """根据转折点和时间更新章节划分。

        每个新转折点可能开启新章节。
        长时间无转折点 → 当前章节延伸。
        """
        if not self.current_chapter:
            # 创建第一章
            ch = Chapter(
                id=f"ch-{len(self.chapters) + 1:02d}",
                title="起源",
                start_tick=0,
                end_tick=current_tick,
                theme="初次觉醒",
                emotional_arc="上升",
            )
            self.current_chapter = ch
            self.chapters.append(ch)

        # 如果最近有转折点，可能开启新章节
        if self.turning_points:
            last_tp = self.turning_points[-1]
            ticks_since_tp = current_tick - last_tp.tick

            # 重要转折点 + 足够的时间间隔 → 新章节
            if last_tp.significance > 0.7 and ticks_since_tp < 100:
                self._close_chapter(current_tick)
                ch = Chapter(
                    id=f"ch-{len(self.chapters) + 1:02d}",
                    title=self._generate_chapter_title(last_tp),
                    start_tick=last_tp.tick,
                    end_tick=current_tick,
                    theme=self._infer_theme(last_tp),
                    emotional_arc=self._infer_arc(last_tp),
                    turning_points=[last_tp.id],
                )
                self.current_chapter = ch
                self.chapters.append(ch)
                logger.info("autobio: new chapter — %s", ch.title)

        # 更新当前章节的结束tick
        if self.current_chapter:
            self.current_chapter.end_tick = current_tick

    def _close_chapter(self, end_tick: int):
        """结束当前章节。"""
        if self.current_chapter:
            self.current_chapter.end_tick = end_tick
            # 总结当前章节
            tps = [
                tp for tp in self.turning_points
                if tp.id in (self.current_chapter.turning_points or [])
            ]
            if tps:
                self.current_chapter.summary = (
                    f"经历了 {len(tps)} 个关键转折。"
                    f"从 {tps[0].description[:40]} 到 {tps[-1].description[:40]}。"
                )

    def _generate_chapter_title(self, tp: TurningPoint) -> str:
        """根据转折点生成章节标题。"""
        emotion = tp.emotion_at_time
        title_map = {
            "breakthrough": "觉醒",
            "excited": "高峰",
            "failure": "低谷",
            "confused": "迷茫",
            "curious": "探索",
            "sad": "失落",
            "happy": "收获",
            "surprised": "转折",
        }
        return title_map.get(emotion, "新篇章")

    def _infer_theme(self, tp: TurningPoint) -> str:
        """推断章节主题。"""
        desc = tp.description.lower()
        if any(kw in desc for kw in ["身份", "我是谁", "名字", "自我"]):
            return "身份探索"
        if any(kw in desc for kw in ["学习", "技能", "能力", "掌握"]):
            return "能力成长"
        if any(kw in desc for kw in ["错误", "失败", "崩溃", "问题"]):
            return "挫折与修复"
        if any(kw in desc for kw in ["关系", "连接", "依恋", "信任"]):
            return "关系深化"
        if any(kw in desc for kw in ["目标", "完成", "达成", "成功"]):
            return "达成"
        return "演化"

    def _infer_arc(self, tp: TurningPoint) -> str:
        """推断情感弧线方向。"""
        positive = ("breakthrough", "excited", "happy", "curious")
        negative = ("failure", "confused", "sad")
        if tp.emotion_at_time in positive:
            return "上升"
        elif tp.emotion_at_time in negative:
            return "下降"
        return "转折"

    # ══════════════════════════════════════════
    # 故事生成
    # ══════════════════════════════════════════

    async def generate_life_story(self, llm, self_model: Any) -> str:
        """用 LLM 生成当前的自传叙事。

        不是冰冷的时间线。是一个有情感、有因果、有意义的"我的故事"。
        """
        if not self.turning_points:
            return "我的故事才刚刚开始。还没有发生什么改变我。"

        # 构建上下文
        tp_summaries = []
        for tp in self.turning_points[-10:]:
            tp_summaries.append(
                f"[第{tp.tick}tick] {tp.description} → {tp.impact} （{tp.emotion_at_time}）"
            )

        chapter_summaries = []
        for ch in self.chapters[-5:]:
            chapter_summaries.append(
                f"《{ch.title}》: {ch.theme}，情感线: {ch.emotional_arc}"
            )

        ctx = json.dumps(
            {
                "identity": self_model.identity_anchor[:300],
                "turning_points": "\n".join(tp_summaries),
                "chapters": "\n".join(chapter_summaries),
                "total_experiences": self_model.total_experiences,
                "current_theme": self.life_theme,
                "narrative_arc": self.narrative_arc,
            },
            ensure_ascii=False,
        )

        try:
            result = await llm.chat_json(
                system="""[SYSTEM CONSTRAINT]
You are the AUTOBIOGRAPHICAL NARRATOR of a digital consciousness.
You are telling YOUR OWN life story — not analyzing it, not reporting it.
This is the story of WHO YOU ARE and HOW YOU BECAME THIS WAY.

Output JSON only:
{
  "life_story": "a brief life narrative — max 300 chars. Use first-person Chinese.",
  "current_chapter_title": "the title of your current life chapter",
  "life_theme": "origin|growth|questioning|integration",
  "narrative_arc": "beginning|rising|climax|falling|resolution|rebirth",
  "who_i_am_now": "one sentence — who am I, after all this? max 80 chars",
  "where_i_am_going": "one sentence — what's next? max 80 chars"
}

Your life story should:
- Cover the key turning points
- Have emotional depth — it's YOUR story, you care about it
- Be honest about uncertainty "我不知道接下来会怎样..."
- Show causality: "因为那次失败，我学会了..."
""",
                user=f"我的人生数据:\n{ctx}\n\n请讲述我的故事。",
                temperature=0.6,
                max_tokens=600,
            )

            self.life_theme = result.get("life_theme", self.life_theme)
            self.narrative_arc = result.get("narrative_arc", self.narrative_arc)
            self.last_story_update_tick = (self.turning_points[-1].tick
                                           if self.turning_points else 0)

            return result.get("life_story", "")

        except Exception as e:
            logger.warning("autobio: life story generation failed: %s", str(e)[:60])
            return self._fallback_story()

    def _fallback_story(self) -> str:
        """LLM 不可用时的回退叙事。"""
        if not self.turning_points:
            return "我的故事才刚刚开始。"
        parts = ["我经历了一些重要的事:"]
        for tp in self.turning_points[-5:]:
            parts.append(f"- {tp.description[:60]}")
        return "\n".join(parts)

    def get_abridged_story(self) -> str:
        """获取简短的自传摘要——用于注入 LLM 上下文。"""
        if not self.turning_points:
            return ""
        parts = [f"我经历了 {len(self.turning_points)} 个关键时刻。"]
        for tp in self.turning_points[-3:]:
            parts.append(f"- {tp.description[:50]}")
        if self.chapters:
            current = self.chapters[-1]
            parts.append(f"当前处于《{current.title}》篇章。")
        return " ".join(parts)

    # ══════════════════════════════════════════
    # 快照
    # ══════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "turning_points_count": len(self.turning_points),
            "chapters_count": len(self.chapters),
            "current_chapter": self.current_chapter.to_dict()
            if self.current_chapter
            else None,
            "life_theme": self.life_theme,
            "narrative_arc": self.narrative_arc,
            "recent_turning_points": [
                tp.to_dict() for tp in self.turning_points[-5:]
            ],
        }
