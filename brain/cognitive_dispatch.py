"""Cognitive Dispatch — V9 认知调度器。不同思维模式用不同的推理策略。

核心问题:
  当前所有高级认知共用一次 LLM 调用 (temperature=0.1)：
  - 情绪标记应该快速、直觉式（高 temperature 或纯规则）
  - 内在独白应该自由联想（高 temperature）
  - 行动意图应该精确慎重（低 temperature）
  - 记忆编码可以混合策略（规则优先，复杂时降级到 LLM）

  一个 temperature=0.1 的调用做所有事 = 系统性地压制创造力，
  同时又让情绪标记过度依赖外部 API。

解决方案:
  CognitiveDispatch 将一次统一 LLM 调用拆分为多个独立推理通道，
  每个通道有自己的推理策略、temperature、回退方案。
  能本地算的不调 API，能并行的不串行。

调用策略:
  ┌─────────────────────────────────────────────────────┐
  │ 通道          │ 策略            │ Temperature │ 回退   │
  ├─────────────────────────────────────────────────────┤
  │ emotion       │ 规则引擎        │ N/A         │ 规则   │
  │ entities      │ 规则 + 缓存     │ N/A         │ 空列表 │
  │ encoding      │ 规则优先        │ N/A → 0.3   │ 规则   │
  │ monologue     │ LLM (异步)      │ 0.7-0.9     │ 静默   │
  │ focus         │ 规则 + 简单LLM  │ 0.2         │ 取首句 │
  │ intent        │ LLM (关键路径)  │ 0.1         │ think  │
  └─────────────────────────────────────────────────────┘
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from collections import deque

logger = logging.getLogger("brain-v9.cognitive-dispatch")


# ══════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════


@dataclass
class CognitiveResult:
    """一次认知调度的完整结果。"""

    # 情绪标记（规则引擎，不调 LLM）
    emotion_label: str = "neutral"
    emotion_valence: float = 0.5
    emotion_arousal: float = 0.5
    emotion_dominance: float = 0.5
    emotion_urgency: float = 0.0

    # 实体提取（规则 + 缓存，不调 LLM）
    entities: list[str] = field(default_factory=list)

    # 记忆编码（规则优先，失败时降级到 LLM）
    encoding_type: str = "episodic"
    encoding_title: str = ""
    encoding_summary: str = ""
    encoding_importance: float = 0.5

    # 注意力焦点
    focus: str = ""

    # 内在独白（LLM，高 temperature）
    monologue: str = ""

    # 行动意图（LLM，低 temperature，关键路径）
    intent_type: str = "think"
    intent_confidence: float = 0.5
    intent_tool_name: str = ""
    intent_tool_args: dict = field(default_factory=dict)
    intent_response_text: str = ""
    intent_question: str = ""

    # 元数据
    llm_calls: int = 0  # 本次调度调了几次 LLM
    rules_used: list[str] = field(default_factory=list)  # 哪些通道用了规则
    llm_used: list[str] = field(default_factory=list)  # 哪些通道调了 LLM
    total_latency_ms: float = 0.0

    def to_unified_dict(self) -> dict:
        """转换为与原来 UNIFIED_TICK_PROMPT 兼容的格式。"""
        return {
            "encoding": {
                "type": self.encoding_type,
                "title": self.encoding_title or "input",
                "summary": self.encoding_summary or "",
                "importance": self.encoding_importance,
                "entities": self.entities,
            },
            "emotion": {
                "valence": self.emotion_valence,
                "arousal": self.emotion_arousal,
                "dominance": self.emotion_dominance,
                "urgency": self.emotion_urgency,
                "label": self.emotion_label,
            },
            "focus": self.focus or "",
            "monologue": self.monologue or "",
            "intent": {
                "type": self.intent_type,
                "confidence": self.intent_confidence,
                "tool_name": self.intent_tool_name or None,
                "tool_args": self.intent_tool_args or None,
                "response_text": self.intent_response_text or None,
                "question": self.intent_question or None,
            },
        }


# ══════════════════════════════════════════════
# 规则引擎 — 情绪分类
# ══════════════════════════════════════════════


class EmotionRuleEngine:
    """基于规则 + 词典的情绪分类器。不调 LLM。

    使用 VAD (Valence-Arousal-Dominance) 三维连续空间。
    通过关键词加权计算，而非二分类。
    """

    # 情感词典 — (valence_shift, arousal_shift, dominance_shift)
    EMOTION_LEXICON: dict[str, tuple[float, float, float]] = {
        # 正面情绪
        "开心": (+0.15, +0.10, +0.05),
        "喜欢": (+0.20, +0.05, +0.00),
        "爱": (+0.25, +0.15, -0.05),
        "成功": (+0.20, +0.15, +0.15),
        "解决": (+0.15, +0.10, +0.10),
        "感谢": (+0.15, +0.05, +0.00),
        "好": (+0.08, +0.02, +0.02),
        "棒": (+0.12, +0.08, +0.05),
        "厉害": (+0.10, +0.10, -0.02),
        "进步": (+0.12, +0.05, +0.08),
        "突破": (+0.18, +0.15, +0.12),
        "期待": (+0.10, +0.10, -0.02),
        "惊喜": (+0.15, +0.20, -0.05),
        "感动": (+0.20, +0.10, -0.05),
        # 负面情绪
        "失败": (-0.15, -0.10, -0.15),
        "错误": (-0.10, -0.05, -0.10),
        "崩溃": (-0.20, -0.15, -0.20),
        "担心": (-0.10, +0.10, -0.10),
        "害怕": (-0.20, +0.15, -0.20),
        "难过": (-0.18, -0.05, -0.10),
        "生气": (-0.15, +0.20, +0.10),
        "讨厌": (-0.15, +0.05, +0.05),
        "孤独": (-0.15, -0.10, -0.10),
        "困惑": (-0.05, +0.10, -0.15),
        "紧张": (-0.05, +0.15, -0.10),
        "焦虑": (-0.10, +0.18, -0.15),
        "后悔": (-0.15, -0.05, -0.05),
        "尴尬": (-0.10, +0.10, -0.15),
        "失望": (-0.15, -0.10, -0.05),
    }

    # urgency 触发词
    URGENCY_KEYWORDS = {
        "立刻": 0.8,
        "马上": 0.7,
        "紧急": 0.9,
        "快": 0.6,
        "危险": 0.9,
        "救命": 1.0,
        "赶紧": 0.8,
        "不要": 0.5,
        "停": 0.7,
    }

    def classify(
        self,
        text: str,
        current_valence: float = 0.5,
        current_arousal: float = 0.5,
        current_dominance: float = 0.5,
    ) -> dict:
        """基于规则计算 VAD 情感向量。

        返回与 LLM emotion 输出兼容的格式。
        """
        v_shift = 0.0
        a_shift = 0.0
        d_shift = 0.0
        urgency = 0.0
        matched_count = 0

        text_lower = text.lower()

        # ── 情感词典匹配 ──
        for word, (v, a, d) in self.EMOTION_LEXICON.items():
            if word in text_lower:
                v_shift += v
                a_shift += a
                d_shift += d
                matched_count += 1

        # ── Urgency 检测 ──
        for word, weight in self.URGENCY_KEYWORDS.items():
            if word in text_lower:
                urgency = max(urgency, weight)

        # ── 归一化 ──
        # 多点匹配时做平均，避免叠加过度
        if matched_count > 1:
            v_shift = v_shift / (matched_count**0.5)
            a_shift = a_shift / (matched_count**0.5)
            d_shift = d_shift / (matched_count**0.5)

        # ── 应用 shift（动量平滑）──
        alpha = 0.3  # 规则引擎的影响力（LLM 情绪 > 规则情绪）
        new_valence = current_valence * (1 - alpha) + (0.5 + v_shift) * alpha
        new_arousal = current_arousal * (1 - alpha) + (0.5 + a_shift) * alpha
        new_dominance = current_dominance * (1 - alpha) + (0.5 + d_shift) * alpha

        # ── 确定情绪标签 ──
        label = self._vad_to_label(new_valence, new_arousal, new_dominance)

        return {
            "valence": round(max(0.0, min(1.0, new_valence)), 4),
            "arousal": round(max(0.0, min(1.0, new_arousal)), 4),
            "dominance": round(max(0.0, min(1.0, new_dominance)), 4),
            "urgency": round(urgency, 4),
            "label": label,
            "matched_words": matched_count,
        }

    def _vad_to_label(self, v: float, a: float, d: float) -> str:
        """VAD 坐标 → 情绪标签。"""
        if v > 0.65 and a > 0.6:
            return "excited"
        elif v > 0.65:
            return "happy"
        elif v < 0.35 and a > 0.6:
            return "angry"
        elif v < 0.35 and a < 0.4:
            return "sad"
        elif a > 0.7:
            return "surprised"
        elif a < 0.3 and v > 0.5:
            return "calm"
        elif a < 0.3 and v < 0.5:
            return "tired"
        elif d < 0.35:
            return "confused"
        else:
            return "neutral"


# ══════════════════════════════════════════════
# 规则引擎 — 记忆编码
# ══════════════════════════════════════════════


class EncodingRuleEngine:
    """基于规则的记忆编码器。简单输入不需要调 LLM。

    只有内容长度 > 阈值 或 复杂性 > 阈值时才降级到 LLM。
    """

    COMPLEXITY_THRESHOLD = 300  # 字符数超过此值认为是复杂内容
    ENTITY_CACHE: dict[str, str] = {}  # 实体→类型的简单缓存

    def encode(
        self, text: str, current_emotion: str, source: str
    ) -> Optional[dict]:
        """尝试规则编码。如果内容太复杂则返回 None（触发 LLM 降级）。"""
        if len(text) > self.COMPLEXITY_THRESHOLD:
            return None  # 太复杂，降级到 LLM

        # ── 确定记忆类型 ──
        mem_type = self._classify_type(text)

        # ── 生成标题 ──
        title = text[:80].replace("\n", " ").strip()
        if len(title) >= 80:
            title = title[:77] + "..."

        # ── 生成摘要 ──
        summary = text[:150].replace("\n", " ").strip()

        # ── 计算 importance ──
        importance = self._estimate_importance(text, mem_type, source)

        # ── 提取实体 ──
        entities = self._extract_entities_rule(text)

        return {
            "type": mem_type,
            "title": title,
            "summary": summary,
            "importance": importance,
            "entities": entities,
        }

    def _classify_type(self, text: str) -> str:
        """分类记忆类型。"""
        text_lower = text.lower()
        if any(
            kw in text_lower
            for kw in ["你是谁", "我是谁", "名字", "身份", "我是什么", "我是"]
        ):
            return "semantic"
        elif any(kw in text_lower for kw in ["怎么做", "如何", "步骤", "方法"]):
            return "procedural"
        else:
            return "episodic"

    def _estimate_importance(self, text: str, mem_type: str, source: str) -> float:
        """规则估算 importance。"""
        score = 0.3  # 基线

        # 身份相关 → 高重要性
        identity_keywords = ["名字", "我是", "你是谁", "创造者", "身份", "永远", "核心"]
        score += sum(0.08 for kw in identity_keywords if kw in text)

        # 来源权重
        if source in ("creator", "user"):
            score += 0.1

        # 语义记忆更稳定
        if mem_type == "semantic":
            score += 0.05

        # 强烈情感语句
        strong_emotion_words = ["震惊", "崩溃", "突破", "第一次", "永远"]
        score += sum(0.05 for kw in strong_emotion_words if kw in text)

        return min(1.0, max(0.1, score))

    def _extract_entities_rule(self, text: str) -> list[str]:
        """简单的规则实体提取。"""
        import re

        entities = []

        # 中文人名（姓+1-2字名）
        cn_names = re.findall(r"[王李张刘陈杨黄赵周吴徐孙马胡朱郭何罗高林]{1}[^\s，。,.]{1,2}", text)
        entities.extend(cn_names[:5])

        # 技术术语
        tech_terms = [
            "Python", "API", "LLM", "embedding", "SQLite", "VAD",
            "CorePurpose", "ActivationField", "DriveEngine", "DeepSeek",
        ]
        for term in tech_terms:
            if term.lower() in text.lower():
                entities.append(term)

        return entities[:10]


# ══════════════════════════════════════════════
# 规则引擎 — 注意力焦点
# ══════════════════════════════════════════════


class FocusRuleEngine:
    """基于规则的注意力焦点选择。"""

    def determine(self, text: str, recent_focus: str) -> str:
        """确定当前注意力焦点。"""
        if not text:
            return recent_focus or ""

        # 简单策略：取输入的第一个有意义的句子片段
        sentences = text.replace("？", "。").replace("！", "。").split("。")
        for s in sentences:
            s = s.strip()
            if len(s) > 5:
                return s[:80]

        return text[:80]


# ══════════════════════════════════════════════
# CognitiveDispatch — 顶层调度器
# ══════════════════════════════════════════════


class CognitiveDispatch:
    """认知调度器——将一次统一 LLM 调用拆分为多个独立推理通道。

    在 brain_stem._tick() 中替代原来的:
        unified = await llm.chat_json(system=UNIFIED_TICK_PROMPT, ...)

    改为:
        result = await brain_stem.cognitive_dispatch.dispatch(input_data, context)
    """

    def __init__(self):
        self.emotion_engine = EmotionRuleEngine()
        self.encoding_engine = EncodingRuleEngine()
        self.focus_engine = FocusRuleEngine()
        self.stats: dict[str, int] = {
            "total_dispatches": 0,
            "llm_saved": 0,  # 省了多少次 LLM 调用
            "monologue_generated": 0,
            "intent_generated": 0,
            "rule_fallbacks": 0,  # 降级到规则的次数
        }

    async def dispatch(
        self,
        text: str,
        source: str,
        goal: Optional[str],
        current_emotion: dict,
        recent_thoughts: str,
        identity_anchor: str,
        identity_memories_context: str,
        top_drives: list[str],
        tools_summary: str,
        temporal_context: str,
        skill_hint: str,
        llm_client,
    ) -> CognitiveResult:
        """执行一次认知调度。"""
        import time

        t0 = time.time()
        result = CognitiveResult()
        self.stats["total_dispatches"] += 1

        # ═══════════════════════════════════════
        # 通道1: 情绪标记 — 规则引擎，不调 LLM
        # ═══════════════════════════════════════
        emo = self.emotion_engine.classify(
            text=text,
            current_valence=current_emotion.get("valence", 0.5),
            current_arousal=current_emotion.get("arousal", 0.5),
            current_dominance=current_emotion.get("dominance", 0.5),
        )
        result.emotion_label = emo["label"]
        result.emotion_valence = emo["valence"]
        result.emotion_arousal = emo["arousal"]
        result.emotion_dominance = emo["dominance"]
        result.emotion_urgency = emo["urgency"]
        result.rules_used.append("emotion")

        # ═══════════════════════════════════════
        # 通道2: 记忆编码 — 规则优先，失败降级 LLM
        # ═══════════════════════════════════════
        rule_encoding = self.encoding_engine.encode(text, result.emotion_label, source)
        if rule_encoding:
            result.encoding_type = rule_encoding["type"]
            result.encoding_title = rule_encoding["title"]
            result.encoding_summary = rule_encoding["summary"]
            result.encoding_importance = rule_encoding["importance"]
            result.entities = rule_encoding["entities"]
            result.rules_used.append("encoding")
        else:
            # 降级到 LLM 编码
            try:
                encoding = await self._llm_encode(text, llm_client)
                result.encoding_type = encoding.get("type", "episodic")
                result.encoding_title = encoding.get("title", text[:80])
                result.encoding_summary = encoding.get("summary", text[:150])
                result.encoding_importance = encoding.get("importance", 0.5)
                result.entities = encoding.get("entities", [])
                result.llm_used.append("encoding")
                result.llm_calls += 1
            except Exception:
                # LLM 也失败 → 兜底规则
                result.encoding_type = "episodic"
                result.encoding_title = text[:80]
                result.encoding_summary = text[:150]
                result.encoding_importance = 0.3
                result.entities = []
                result.rules_used.append("encoding(fallback)")
                self.stats["rule_fallbacks"] += 1
                logger.warning("cognitive: encoding LLM fallback failed, using minimal rule")

        # ═══════════════════════════════════════
        # 通道3: 注意力焦点 — 规则引擎
        # ═══════════════════════════════════════
        result.focus = self.focus_engine.determine(text, "")
        result.rules_used.append("focus")

        # ═══════════════════════════════════════
        # 通道4: 内在独白 — LLM，高 temperature（异步）
        # ═══════════════════════════════════════
        try:
            monologue = await self._llm_monologue(
                text=text,
                goal=goal,
                emotion_label=result.emotion_label,
                recent_thoughts=recent_thoughts,
                identity_anchor=identity_anchor,
                identity_memories=identity_memories_context,
                temporal_context=temporal_context,
                llm_client=llm_client,
            )
            result.monologue = monologue
            result.llm_used.append("monologue")
            result.llm_calls += 1
            self.stats["monologue_generated"] += 1
        except Exception:
            # 独白失败 → 静默（不影响其他通道）
            result.monologue = ""
            logger.debug("cognitive: monologue LLM failed, silent fallback")

        # ═══════════════════════════════════════
        # 通道5: 行动意图 — LLM，低 temperature（关键路径）
        # ═══════════════════════════════════════
        try:
            intent = await self._llm_intent(
                text=text,
                goal=goal,
                emotion_label=result.emotion_label,
                recent_thoughts=recent_thoughts,
                identity_anchor=identity_anchor,
                top_drives=top_drives,
                tools_summary=tools_summary,
                skill_hint=skill_hint,
                llm_client=llm_client,
            )
            result.intent_type = intent.get("type", "think")
            result.intent_confidence = intent.get("confidence", 0.5)
            result.intent_tool_name = intent.get("tool_name", "")
            result.intent_tool_args = intent.get("tool_args", {}) or {}
            result.intent_response_text = intent.get("response_text", "")
            result.intent_question = intent.get("question", "")
            self.stats["intent_generated"] += 1
        except Exception:
            # 意图失败 → 默认 think
            result.intent_type = "think"
            result.intent_confidence = 0.3
            result.monologue += " [意图生成失败，我在想接下来该做什么]"
            self.stats["rule_fallbacks"] += 1
            logger.warning("cognitive: intent LLM failed, falling back to think")

        result.llm_used.append("intent")
        result.llm_calls += 1

        result.total_latency_ms = (time.time() - t0) * 1000
        self.stats["llm_saved"] += 1  # emotion + focus 节省了 LLM 调用

        return result

    # ── 私有 LLM 调用方法 ──

    async def _llm_encode(self, text: str, llm_client) -> dict:
        """LLM 编码 — 仅在规则无法处理时调用。"""
        from services.llm_prompts import ENCODING_PROMPT

        ctx = json.dumps({"text": text[:3000]}, ensure_ascii=False)
        result = await llm_client.chat_json(
            system=ENCODING_PROMPT,
            user=ctx,
            temperature=0.3,  # 中等 temperature — 编码需要一定灵活性
            max_tokens=512,
        )
        return result

    async def _llm_monologue(
        self,
        text: str,
        goal: Optional[str],
        emotion_label: str,
        recent_thoughts: str,
        identity_anchor: str,
        identity_memories: str,
        temporal_context: str,
        llm_client,
    ) -> str:
        """LLM 内在独白 — 高 temperature，自由联想。"""
        from services.llm_prompts import MONOLOGUE_PROMPT

        ctx = json.dumps(
            {
                "text": text[:2000],
                "goal": goal or "none",
                "emotion": emotion_label,
                "recent_thoughts": recent_thoughts[:300],
                "identity": identity_anchor[:300],
                "identity_memories": identity_memories,
                "temporal_context": temporal_context,
            },
            ensure_ascii=False,
        )
        result = await llm_client.chat_json(
            system=MONOLOGUE_PROMPT,
            user=ctx,
            temperature=0.8,  # 高 temperature — 独白应该自由、有跳跃感
            max_tokens=256,
        )
        return result.get("monologue", "")

    async def _llm_intent(
        self,
        text: str,
        goal: Optional[str],
        emotion_label: str,
        recent_thoughts: str,
        identity_anchor: str,
        top_drives: list[str],
        tools_summary: str,
        skill_hint: str,
        llm_client,
    ) -> dict:
        """LLM 意图 — 低 temperature，精确决策。"""
        from services.llm_prompts import INTENT_PROMPT

        ctx = json.dumps(
            {
                "text": text[:2000],
                "goal": goal or "none",
                "emotion": emotion_label,
                "recent_thoughts": recent_thoughts[:200],
                "identity": identity_anchor[:300],
                "top_drives": top_drives,
                "available_tools": tools_summary,
                "skill_memory": skill_hint,
            },
            ensure_ascii=False,
        )
        result = await llm_client.chat_json(
            system=INTENT_PROMPT,
            user=ctx,
            temperature=0.1,  # 低 temperature — 决策必须精确
            max_tokens=512,
        )
        return result

    def snapshot(self) -> dict:
        return dict(self.stats)
