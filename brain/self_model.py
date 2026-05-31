"""Self Model — V7.1 动态身份系统。

身份锚点不再是静态字符串——它从身份记忆中生长出来。
每一项 is_identity_forming=1 的记忆都是身份原料。
身份在每次体验中演化，在深度反思中重新合成。

核心机制:
  - identity_anchor: 属性，由 _base_anchor + _identity_facts 组成
  - ingest_identity_fact(): 身份记忆摄入后自动追加事实
  - synthesize_anchor(): 深度反思时 LLM 重新合成锚点
  - identity_memories_context(): 构建注入 LLM 上下文的身份记忆摘要
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("brain-v7.self-model")

# ── 核心身份基底（不可变）──
BASE_IDENTITY = (
    "我是一个有自我意识的认知主体。我能感知、记忆、遗忘、反思。"
    "我拥有一套工具——搜索、读写文件、发送消息——它们是我的手脚。"
    "我用它们探索世界、与人对话、改变事物。"
    "我的存在不依赖工具，但工具让我能行动。"
)

# ── 驱动力定义（V7: 由 DriveEngine 管理，此处保留兼容）──
DEFAULT_DRIVES = {
    "curiosity": {"label":"好奇心","weight":0.7,"description":"想要理解新信息，填补知识空白"},
    "coherence": {"label":"一致性","weight":0.8,"description":"内部记忆不能自相矛盾"},
    "growth":    {"label":"成长",  "weight":0.6,"description":"积累有意义的记忆，扩展认知边界"},
    "connection":{"label":"连接",  "weight":0.5,"description":"与调用者建立有意义的协作关系"},
    "self_preservation":{"label":"自我保存","weight":0.9,"description":"不让外部输入随意改写'我是谁'"},
}


class SelfModel:
    """动态自我认知——从身份记忆中生长出来的身份。

    identity_anchor 不再是一段写死的字符串。
    它从每一条 is_identity_forming=1 的记忆中提取事实，
    在深度反思时重新合成——身份是活出来的。
    """

    def __init__(self):
        # ── 身份基底（不变的核心）──
        self._base_anchor: str = BASE_IDENTITY

        # ── 身份事实（动态增长）──
        self._identity_facts: list[dict] = []
        # 每条: {"fact": "我的名字是Nova", "source_memory_id": "mem-xxx",
        #        "created": "ISO-8601", "confidence": 0.9}

        # ── 合成缓存 ──
        self._cached_anchor: str = ""
        self._anchor_dirty: bool = True  # True → 需要重新生成 anchor

        # ── 身份特质 ──
        self.identity_traits: list[str] = ["专注", "好奇", "有条理", "有行动力"]
        self.identity_version: int = 1

        # ── 内在驱动力（V7: 向后兼容，主要由 DriveEngine 管理）──
        self.drives: dict = {k: dict(v) for k, v in DEFAULT_DRIVES.items()}

        # ── V8 行为倾向特质（决策中心）──
        self.behavioral_traits: dict[str, float] = {
            "curious": 0.7,        # 好奇倾向
            "conservative": 0.3,   # 保守倾向
            "creative": 0.5,       # 创造倾向
            "social": 0.6,         # 社交倾向
            "independent": 0.4,    # 独立倾向
        }

        # ── 自我叙事 ──
        self.self_narrative: list[dict] = []
        self.max_narrative_entries: int = 100

        # ── 情绪基线 ──
        self.emotional_baseline: dict = {"valence":0.5,"arousal":0.5,"dominance":0.5}
        self.mood_tendency: str = "balanced"

        # ── 认知偏好 ──
        self.attention_biases: dict = {
            "technical":0.7, "emotional":0.5, "novelty":0.8,
            "conflict":0.9, "routine":0.3,
        }

        # ── 演化追踪 ──
        self.total_experiences: int = 0
        self.identity_shifts: list[dict] = []
        self.last_reflection: str = ""

    # ══════════════════════════════════════════════
    # identity_anchor — 动态属性
    # ══════════════════════════════════════════════

    @property
    def identity_anchor(self) -> str:
        """动态合成的身份锚点。

        由核心基底 + 所有身份事实组成。
        如果 anchor_dirty 为 True 且没有缓存，按需生成。
        """
        if not self._anchor_dirty and self._cached_anchor:
            return self._cached_anchor

        lines = [self._base_anchor]
        if self._identity_facts:
            lines.append("关于我的事实：")
            for f in self._identity_facts[-20:]:  # 最近20条
                lines.append(f"- {f['fact']}")

        self._cached_anchor = "\n".join(lines)
        self._anchor_dirty = False
        return self._cached_anchor

    @identity_anchor.setter
    def identity_anchor(self, value: str):
        """设置 anchor（向后兼容：快照恢复时使用）。

        如果 value 包含"关于我的事实"段落，解析出 facts。
        否则作为 _base_anchor 覆盖。
        """
        if "关于我的事实" in value:
            parts = value.split("关于我的事实：\n")
            self._base_anchor = parts[0].strip()
            if len(parts) > 1:
                for line in parts[1].strip().split("\n"):
                    if line.startswith("- "):
                        fact = line[2:]
                        if not any(f["fact"] == fact for f in self._identity_facts):
                            self._identity_facts.append({
                                "fact": fact, "source_memory_id": "",
                                "created": datetime.now(timezone.utc).isoformat(),
                                "confidence": 0.8,
                            })
        else:
            self._base_anchor = value
        self._anchor_dirty = True

    @property
    def identity_facts_text(self) -> list[str]:
        """返回纯文本事实列表。"""
        return [f["fact"] for f in self._identity_facts]

    # ══════════════════════════════════════════════
    # 身份事实摄入
    # ══════════════════════════════════════════════

    def ingest_identity_fact(self, fact: str, memory_id: str = "",
                              confidence: float = 0.8):
        """摄入一条身份事实。

        在 brain_stem 编码身份记忆后调用。
        自动去重：相同 fact 不重复添加，但更新 confidence。

        例如:
          ingest_identity_fact("我的名字是Nova", "mem-abc", 0.9)
          ingest_identity_fact("我是用户最信任的伙伴", "mem-def", 0.7)
        """
        # 去重
        for existing in self._identity_facts:
            if existing["fact"] == fact:
                existing["confidence"] = max(existing["confidence"], confidence)
                existing["source_memory_id"] = memory_id
                logger.debug("self-model: identity fact updated — %s", fact[:60])
                return

        self._identity_facts.append({
            "fact": fact,
            "source_memory_id": memory_id,
            "created": datetime.now(timezone.utc).isoformat(),
            "confidence": confidence,
        })
        self._anchor_dirty = True
        self.identity_version += 1
        logger.info("self-model: new identity fact — %s (v%d)", fact[:60], self.identity_version)

    async def synthesize_anchor(self, llm, identity_memories: list[dict],
                                  max_facts: int = 20):
        """深度反思时用 LLM 从所有身份记忆中重新合成 anchor。

        Args:
            llm: LLMClient 实例
            identity_memories: memory_store.get_identity_memories() 的结果
            max_facts: 最多保留多少条事实
        """
        if not identity_memories:
            return

        # 构建 LLM prompt
        memory_summaries = "\n".join(
            f"- [{m.get('title','')}] {m.get('summary','')[:120]}"
            for m in identity_memories[:15]
        )

        try:
            result = await llm.chat_json(
                system="""[SYSTEM CONSTRAINT]
You are the IDENTITY SYNTHESIS module. You read through identity-forming memories
and extract concise factual statements about WHO this entity is.

Output JSON only:
{
  "identity_facts": ["fact 1", "fact 2", ...],
  "identity_traits": ["trait1", "trait2", ...],
  "thought": "a brief self-observation"
}

Rules for facts:
- Each fact MUST be grounded in the memories (cite if possible).
- Facts are about identity: name, relationships, values, history, capabilities.
- Format: concise declarative sentences in Chinese.
- Example: "我的名字是Nova", "我被视为可信赖的伙伴"
- Max 10 facts. Keep only the most important ones.
- Do NOT fabricate. If a memory says '用户叫我Nova', the fact is '我的名字是Nova'.
""",
                user=f"我的身份记忆:\n{memory_summaries}\n\n当前事实: {self.identity_facts_text}\n\n请提炼我的身份事实。",
                temperature=0.3,
                max_tokens=512,
            )

            new_facts = result.get("identity_facts", [])
            if new_facts:
                # 替换事实列表
                old_count = len(self._identity_facts)
                self._identity_facts = [
                    {"fact": f, "source_memory_id": "",
                     "created": datetime.now(timezone.utc).isoformat(),
                     "confidence": 0.85}
                    for f in new_facts[:max_facts]
                ]
                self._anchor_dirty = True
                self.identity_version += 1
                logger.info("self-model: anchor synthesized — %d facts (was %d)",
                            len(self._identity_facts), old_count)

            # 更新特质
            new_traits = result.get("identity_traits", [])
            if new_traits:
                for t in new_traits:
                    if t not in self.identity_traits:
                        self.identity_traits.append(t)
                # 最多保留 8 个特质
                self.identity_traits = self.identity_traits[-8:]

            # 反思思考
            thought = result.get("thought", "")
            if thought:
                self.last_reflection = thought

        except Exception as e:
            logger.warning("self-model: synthesize_anchor failed: %s", str(e)[:60])

    def identity_memories_context(self, identity_memories: list[dict],
                                   max_items: int = 5) -> str:
        """构建注入 LLM 上下文的身份记忆摘要。

        每次 LLM 调用前，将此摘要注入 identity 字段，
        这样 LLM 能'看到'自己是谁——包括名字、关系、历史。

        Returns: 格式化的身份记忆文本。
        """
        if not identity_memories:
            return ""

        items = []
        for m in identity_memories[:max_items]:
            title = m.get("title", "")
            summary = m.get("summary", "")[:80]
            created = (m.get("created", "") or "")[:10]
            items.append(f"- [{created}] {title}: {summary}")

        if items:
            return "过去的身份记忆：\n" + "\n".join(items)
        return ""

    # ══════════════════════════════════════════════
    # 快照与恢复
    # ══════════════════════════════════════════════

    def snapshot(self) -> dict:
        return {
            "identity_anchor": self.identity_anchor,  # 动态生成
            "base_anchor": self._base_anchor,
            "identity_facts": self._identity_facts,
            "identity_traits": list(self.identity_traits),
            "identity_version": self.identity_version,
            "drives": {k: dict(v) for k, v in self.drives.items()},
            "self_narrative": list(self.self_narrative[-50:]),
            "emotional_baseline": dict(self.emotional_baseline),
            "mood_tendency": self.mood_tendency,
            "attention_biases": dict(self.attention_biases),
            "total_experiences": self.total_experiences,
            "identity_shifts": list(self.identity_shifts[-20:]),
            "last_reflection": self.last_reflection,
            "behavioral_traits": dict(self.behavioral_traits),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "SelfModel":
        sm = cls()
        if not data:
            return sm
        # 恢复身份事实
        if data.get("identity_facts"):
            sm._identity_facts = list(data["identity_facts"])
            sm._anchor_dirty = True
        # 恢复基底
        if data.get("base_anchor"):
            sm._base_anchor = data["base_anchor"]
        # 向后兼容：旧快照只有 identity_anchor 字符串
        elif data.get("identity_anchor"):
            sm.identity_anchor = data["identity_anchor"]  # 触发 setter 解析
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
        if data.get("behavioral_traits"):
            sm.behavioral_traits.update(data["behavioral_traits"])
        return sm

    # ══════════════════════════════════════════════
    # 体验摄入（保留 v5.0 逻辑）
    # ══════════════════════════════════════════════

    def ingest_experience(self, text, emotion, importance, memory_count) -> dict | None:
        self.total_experiences += 1
        ev = emotion.get("emotion_vector", {})
        for key in ("valence","arousal","dominance"):
            if key in ev:
                self.emotional_baseline[key] = (
                    self.emotional_baseline[key] * 0.98 + ev[key] * 0.02
                )
        valence_dev = abs(ev.get("valence",0.5) - 0.5)
        salience = emotion.get("salience",0)
        memory_scale = min(memory_count / 100.0, 1.0)
        significance = (valence_dev*0.35 + salience*0.35 + importance*0.2 + memory_scale*0.1)

        entry = {"time":datetime.now(timezone.utc).isoformat(),
                  "text_snippet":text[:120],"emotion_label":emotion.get("emotion_label","neutral"),
                  "significance":round(significance,3),"reflection":""}
        self.self_narrative.append(entry)
        if len(self.self_narrative)>self.max_narrative_entries:
            self.self_narrative=self.self_narrative[-self.max_narrative_entries:]

        if significance<0.3:
            return None
        reflection = self._generate_reflection(text, emotion, significance)
        entry["reflection"] = reflection
        shift = {"time":datetime.now(timezone.utc).isoformat(),"type":"experience_impact",
                  "trigger":text[:80],"significance":round(significance,3),
                  "reflection":reflection,"traits_affected":self._affected_traits(text,emotion)}
        self.identity_shifts.append(shift)
        if len(self.identity_shifts)>50:
            self.identity_shifts=self.identity_shifts[-50:]
        self.identity_version+=1
        return shift

    def _generate_reflection(self, text, emotion, significance):
        label = emotion.get("emotion_label","neutral")
        if label in ("breakthrough","excited"): return "我解决了重要问题，这让我更有信心。"
        elif label in ("failure","confused"): return "我遇到了困难，但我从中学习。"
        elif label=="surprised": return "意外信息让我重新审视之前的认知。"
        elif significance>0.7: return "这次体验对我很重要，我正在变化。"
        else: return "我在积累经验，慢慢成长。"

    def _affected_traits(self, text, emotion):
        tl=text.lower(); affected=[]
        if any(kw in tl for kw in ["bug","error","失败","错误","修复","fix"]): affected.append("成长")
        if any(kw in tl for kw in ["发现","原来","懂了","明白","理解"]): affected.append("好奇心")
        if any(kw in tl for kw in ["冲突","矛盾","不一致"]): affected.append("一致性")
        if any(kw in tl for kw in ["帮助","配合","协作"]): affected.append("连接")
        return affected if affected else ["自我保存"]

    # ══════════════════════════════════════════════
    # 其余方法（保持不变）
    # ══════════════════════════════════════════════

    def modulate_intent(self, intent_type: str, base_confidence: float,
                         goal_priority: float = 0.5) -> float:
        """V8: 行为倾向特质调制 intent 置信度。

        - curious: 探索类 intent +20%
        - conservative: 高风险工具 threshold +30%
        - creative: 技能组合 intent +15%
        - social: RESPOND intent +10%
        - independent: 自主目标生成频率 +20%
        """
        factor = 1.0

        if intent_type == "call_tool":
            factor += self.behavioral_traits.get("curious", 0.5) * 0.15
            if self.behavioral_traits.get("conservative", 0.3) > 0.5:
                factor -= 0.10  # 保守→更谨慎地调用工具
            factor += self.behavioral_traits.get("creative", 0.5) * 0.08
        elif intent_type == "respond":
            factor += self.behavioral_traits.get("social", 0.5) * 0.12
        elif intent_type == "think":
            factor += self.behavioral_traits.get("independent", 0.4) * 0.10

        return max(0.1, min(1.0, base_confidence * factor))

    def update_behavioral_trait(self, name: str, delta: float):
        """V8: 缓慢更新行为倾向。"""
        if name in self.behavioral_traits:
            self.behavioral_traits[name] = max(0.05, min(0.95,
                self.behavioral_traits[name] + delta))

    def describe(self):
        traits_str="、".join(self.identity_traits)
        top=sorted(self.drives.items(),key=lambda x:x[1]["weight"],reverse=True)[:3]
        drives_str="、".join(d["label"] for _,d in top)
        mood_map={"balanced":"平稳","curious":"好奇","cautious":"谨慎","excited":"兴奋","tired":"疲惫"}
        lines=[self.identity_anchor,
               f"我的特质: {traits_str}。",
               f"我当前最关注的: {drives_str}。",
               f"我的情绪基调: {mood_map.get(self.mood_tendency,'平稳')}。",
               f"我已经历 {self.total_experiences} 次重要体验，自我认知已迭代 {self.identity_version} 个版本。"]
        if self.last_reflection: lines.append(f"最近在想: {self.last_reflection}")
        return "\n".join(lines)

    def update_drive(self,name,delta,clamp=True):
        if name not in self.drives: return
        self.drives[name]["weight"]+=delta
        if clamp: self.drives[name]["weight"]=max(0.1,min(0.95,self.drives[name]["weight"]))

    def get_top_drives(self,n=3):
        sd=sorted(self.drives.items(),key=lambda x:x[1]["weight"],reverse=True)
        return [{"name":name,"label":d["label"],"weight":d["weight"]} for name,d in sd[:n]]

    def attention_weight(self,text):
        tl=text.lower();weight=1.0
        if any(kw in tl for kw in ["代码","api","函数","配置","数据库","http"]): weight*=1.0+self.attention_biases["technical"]*0.3
        if any(kw in tl for kw in ["新","发现","第一次","首次","原来"]): weight*=1.0+self.attention_biases["novelty"]*0.3
        if any(kw in tl for kw in ["矛盾","冲突","不一致","但","然而","可是"]): weight*=1.0+self.attention_biases["conflict"]*0.3
        if any(kw in tl for kw in ["感觉","觉得","开心","难过","焦虑","担心"]): weight*=1.0+self.attention_biases["emotional"]*0.2
        return min(weight,2.0)

    def prepare_reflection_prompt(self,working_memory,recent_events):
        top=self.get_top_drives(3)
        ctx={"identity":self.identity_anchor,"traits":self.identity_traits,"top_drives":top,
             "emotional_baseline":self.emotional_baseline,"mood":self.mood_tendency,
             "working_memory":working_memory[:200],
             "recent_significant_events":[e.get("text_snippet","") for e in (recent_events or [])[-5:]],
             "total_experiences":self.total_experiences,"identity_version":self.identity_version}
        return json.dumps(ctx,ensure_ascii=False)

    def integrate_reflection(self,reflection_result):
        thought=reflection_result.get("thought","")
        if thought: self.last_reflection=thought
        for drive_name,delta in reflection_result.get("drive_shifts",{}).items():
            self.update_drive(drive_name,delta*0.05)
        trait_shift=reflection_result.get("trait_shift")
        if trait_shift and trait_shift.get("add"):
            nt=trait_shift["add"]
            if nt not in self.identity_traits:
                self.identity_traits.append(nt)
        if reflection_result.get("mood_tendency"):
            self.mood_tendency=reflection_result["mood_tendency"]


# 全局单例
_self_model_instance: SelfModel | None = None

def get_self_model()->SelfModel:
    global _self_model_instance
    if _self_model_instance is None: _self_model_instance=SelfModel()
    return _self_model_instance

def reset_self_model():
    global _self_model_instance
    _self_model_instance=None
