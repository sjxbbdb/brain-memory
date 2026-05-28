"""Auto-Ingest 服务 — 从原始对话文本自动推断记忆参数。

任何智能体只需 POST 一段文本，本服务自动：
1. 推断记忆类型（episodic/semantic/procedural/narrative）
2. 检测情绪信号并估计重要性
3. 估计新颖度和失败代价
4. 生成标题
5. 返回 MemoryCreate 对象供门控判定

设计原则：零外部依赖，纯规则引擎。不调 LLM，确保任何语言/平台的 Agent 都能调用。
"""
import re
from models.schemas import MemoryCreate
from config import (
    EMOTIONAL_KEYWORDS_FAILURE,
    EMOTIONAL_KEYWORDS_BREAKTHROUGH,
    EMOTIONAL_KEYWORDS_CORRECTION,
    EMOTIONAL_KEYWORDS_CONFLICT,
)
from services.entity_extractor import extract_entities, extract_emotion_tags

PROCEDURAL_PATTERNS = [
    r"步骤", r"流程", r"如何", r"怎么", r"配置", r"安装", r"部署",
    r"命令", r"执行", r"运行", r"启动", r"step", r"how.?to",
    r"setup", r"install", r"deploy", r"command", r"run ",
    r"启动方式", r"使用方法", r"操作",
]

SEMANTIC_PATTERNS = [
    r"定义", r"概念", r"原理", r"架构", r"是(什么|一种|一个)",
    r"位于", r"路径", r"地址", r"端口", r"definition", r"concept",
    r"architecture", r"means", r"refers to",
]

NARRATIVE_PATTERNS = [
    r"我是", r"我作为", r"我的", r"成长", r"身份", r"故事",
    r"经历", r"回忆", r"I am", r"my role", r"identity",
    r"growth", r"journey",
]

HIGH_IMPORTANCE_PATTERNS = [
    r"重要", r"关键", r"核心", r"必须", r"绝不", r"永远",
    r"记住", r"注意", r"important", r"critical", r"essential",
    r"must", r"never", r"remember", r"关键发现", r"根因",
]

MEDIUM_IMPORTANCE_PATTERNS = [
    r"建议", r"推荐", r"应该", r"最好", r"recommend", r"should",
    r"better", r"prefer",
]

HIGH_FAILURE_COST_PATTERNS = [
    r"崩溃", r"损坏", r"丢失", r"不可逆", r"生产", r"线上",
    r"crash", r"corrupt", r"lose", r"irreversible",
    r"production", r"数据", r"data.?loss",
]


def infer_type(text: str) -> str:
    text_lower = text.lower()
    scores = {"episodic": 1, "semantic": 0, "procedural": 0, "narrative": 0}
    for p in PROCEDURAL_PATTERNS:
        if re.search(p, text_lower): scores["procedural"] += 1
    for p in SEMANTIC_PATTERNS:
        if re.search(p, text_lower): scores["semantic"] += 1
    for p in NARRATIVE_PATTERNS:
        if re.search(p, text_lower): scores["narrative"] += 1
    event = [r"做了", r"发现", r"修了", r"改了", r"创建", r"完成", r"解决",
             r"did", r"found", r"fixed", r"changed", r"created", r"completed", r"solved"]
    for p in event:
        if re.search(p, text_lower): scores["episodic"] += 1
    return max(scores, key=scores.get)


def estimate_importance(text: str) -> float:
    tl = text.lower()
    for p in HIGH_IMPORTANCE_PATTERNS:
        if re.search(p, tl): return min(0.75 + len(text) / 5000 * 0.15, 0.90)
    for p in MEDIUM_IMPORTANCE_PATTERNS:
        if re.search(p, tl): return min(0.55 + len(text) / 5000 * 0.10, 0.65)
    return min(0.35 + len(text) / 3000 * 0.25, 0.60)


def estimate_novelty(text: str) -> float:
    tl = text.lower()
    high = [r"新(的|发现|方法|方案|思路)", r"首次", r"第一次", r"从未", r"突破",
            r"new", r"first.?time", r"never.?before", r"breakthrough", r"novel"]
    for p in high:
        if re.search(p, tl): return 0.75
    if len(text) > 200: return 0.50
    if len(text) > 100: return 0.40
    return 0.30


def estimate_failure_cost(text: str) -> float:
    tl = text.lower()
    for p in HIGH_FAILURE_COST_PATTERNS:
        if re.search(p, tl): return 0.75
    ops = [r"配置", r"config", r"路径", r"path", r"端口", r"port",
           r"key", r"密码", r"password", r"token", r"环境变量", r"env"]
    for p in ops:
        if re.search(p, tl): return 0.55
    return 0.35


def estimate_goal_relevance(text: str) -> float:
    tl = text.lower()
    goal = [r"目标", r"目的", r"要(实现|达成|完成)", r"goal", r"objective", r"aim", r"purpose"]
    for p in goal:
        if re.search(p, tl): return 0.70
    action = [r"决定", r"选择", r"采用", r"使用", r"方案", r"decide", r"choose", r"adopt", r"plan"]
    for p in action:
        if re.search(p, tl): return 0.55
    return 0.40


def detect_emotion(text: str) -> tuple:
    tl = text.lower()
    if any(kw.lower() in tl for kw in EMOTIONAL_KEYWORDS_BREAKTHROUGH):
        return 0.80, "breakthrough"
    if any(kw.lower() in tl for kw in EMOTIONAL_KEYWORDS_FAILURE):
        return 0.75, "failure"
    if any(kw.lower() in tl for kw in EMOTIONAL_KEYWORDS_CONFLICT):
        return 0.70, "conflict"
    if any(kw.lower() in tl for kw in EMOTIONAL_KEYWORDS_CORRECTION):
        return 0.65, "correction"
    return 0.30, "neutral"


def generate_title(text: str, inferred_type: str) -> str:
    clean = text.strip()
    for sep in ["。", ". ", chr(10), "；"]:
        if sep in clean:
            title = clean.split(sep)[0].strip()
            if len(title) > 4: return title[:80]
    return clean[:60] + ("..." if len(clean) > 60 else "")


def _extract_tags(text: str, source: str) -> list:
    tags = [f"source:{source}"]
    tl = text.lower()
    tech = {"python": "python", "javascript": "javascript", "typescript": "typescript",
            "api": "api", "database": "database", "sql": "sql", "git": "git",
            "docker": "docker", "linux": "linux", "windows": "windows",
            "mcp": "mcp", "agent": "agent", "memory": "memory",
            "config": "configuration", "bug": "bug", "fix": "fix",
            "deploy": "deployment", "test": "testing"}
    for kw, tag in tech.items():
        if kw in tl: tags.append(tag)
    if any(kw in tl for kw in ["错误", "失败", "bug", "error", "fail"]): tags.append("error")
    if any(kw in tl for kw in ["解决", "修复", "fixed", "solved"]): tags.append("resolved")
    if any(kw in tl for kw in ["决定", "选择", "decide", "choose"]): tags.append("decision")
    return tags[:8]




# ── 去重 ──

def _text_fingerprint(text: str) -> str:
    """生成文本指纹：提取关键实体 + 前30个有效字符的哈希。"""
    import hashlib
    # 提取中英文关键词
    words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text.lower())
    key = " ".join(sorted(set(words)))[:200]
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _title_overlap(title: str, existing_titles: list[str]) -> float:
    """计算标题与已有记忆的字符重叠率。"""
    if not existing_titles:
        return 0.0
    max_overlap = 0.0
    title_chars = set(title)
    for ext in existing_titles:
        ext_chars = set(ext)
        if not ext_chars:
            continue
        overlap = len(title_chars & ext_chars) / len(title_chars | ext_chars)
        max_overlap = max(max_overlap, overlap)
    return max_overlap


async def check_duplicate(text: str, title: str, threshold: float = 0.55) -> dict:
    """三阶段去重检测：
    1. 精确内容哈希 (O(1))
    2. 前60字符前缀匹配 (对短文本非常有效)
    3. 实体重叠率 > 0.7 (基于v2.1实体提取)

    返回 {"is_duplicate": bool, "matched_id": str|None, "overlap": float}
    """
    import hashlib, json
    from models.database import get_db
    from services.entity_extractor import extract_entities
    
    text_hash = hashlib.md5(text.strip().encode()).hexdigest()
    text_prefix = text.strip()[:60].lower()
    new_entities = set(extract_entities(text)[0])
    
    db = await get_db()
    try:
        # 查最近 100 条未归档记忆
        cursor = await db.execute(
            "SELECT id, title, content, entities FROM memories WHERE archived = 0 ORDER BY created DESC LIMIT 100"
        )
        rows = await cursor.fetchall()
        
        for row in rows:
            # Stage 1: 精确内容哈希
            existing_hash = hashlib.md5((row["content"] or "").strip().encode()).hexdigest()
            if existing_hash == text_hash:
                return {"is_duplicate": True, "matched_id": row["id"], "overlap": 1.0}
            
            # Stage 2: 前60字符前缀匹配
            existing_prefix = (row["content"] or "").strip()[:60].lower()
            if existing_prefix == text_prefix and len(text_prefix) >= 20:
                return {"is_duplicate": True, "matched_id": row["id"], "overlap": 0.95}
            
            # Stage 3: 实体重叠率
            if new_entities:
                existing_entities = set(json.loads(row["entities"] or "[]"))
                if existing_entities:
                    overlap = len(new_entities & existing_entities) / max(len(new_entities | existing_entities), 1)
                    if overlap > 0.7 and len(text) < 200:
                        return {"is_duplicate": True, "matched_id": row["id"], "overlap": round(overlap, 2)}
        
        return {"is_duplicate": False, "matched_id": None, "overlap": 0.0}
    finally:
        await db.close()

def auto_ingest(text: str, source: str = "unknown",
                explicit_mark: bool = False,
                metadata: dict | None = None):
    """主入口：从原始文本自动构建 MemoryCreate。"""
    inferred_type = infer_type(text)
    surprise_score, emotion_label = detect_emotion(text)
    importance = estimate_importance(text)
    if explicit_mark: importance = max(importance, 0.85)
    novelty = estimate_novelty(text)
    failure_cost = estimate_failure_cost(text)
    goal_relevance = estimate_goal_relevance(text)
    if metadata:
        if metadata.get("current_goal"): goal_relevance = min(goal_relevance + 0.20, 1.0)
        if metadata.get("high_stakes"): failure_cost = min(failure_cost + 0.25, 1.0)
    title = generate_title(text, inferred_type)
    tags = _extract_tags(text, source)
    # v2.1: auto-extract entities and relations
    entities, relations = extract_entities(text)
    emotion_tags = extract_emotion_tags(text)
    for t in emotion_tags:
        if t not in tags:
            tags.append(t)
    return MemoryCreate(
        title=title, content=text[:2000], type=inferred_type,
        importance=round(importance, 2), novelty=round(novelty, 2),
        failure_cost=round(failure_cost, 2), goal_relevance=round(goal_relevance, 2),
        surprise_score=round(surprise_score, 2), source=source,
        explicit_mark=explicit_mark, tags=tags,
        entities=entities, relations=relations,
    )
