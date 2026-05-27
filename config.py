# Brain Memory System — 配置常量
# 所有可调参数集中于此

# ── 数据库 ──
DB_PATH = "brain_memory.db"

# ── 情绪权重公式系数 ──
EMOTION_WEIGHTS = {
    "importance": 0.30,
    "failure_cost": 0.25,
    "novelty": 0.20,
    "goal_relevance": 0.15,
    "surprise_score": 0.10,
}

# ── 认知检索公式系数 ──
RETRIEVAL_WEIGHTS = {
    "semantic_similarity": 0.25,
    "goal_relevance": 0.25,
    "emotion_weight": 0.20,
    "temporal_proximity": 0.15,
    "causal_relevance": 0.15,
}

# ── 衰减参数 ──
DECAY_RATE_DEFAULT = 0.05
HALF_LIFE_DEFAULT = 30
DECAY_REDUCTION_FACTOR = 0.85       # 每次检索后 decay_rate *= 0.85
HALF_LIFE_EXTENSION_FACTOR = 1.15   # 每次检索后 half_life *= 1.15
IMPORTANCE_BOOST = 0.01             # 每次检索后 importance += 0.01

# ── 巩固参数 ──
EVICTION_THRESHOLD = 0.10           # emotion_weight 低于此值触发清理
STALENESS_THRESHOLD = 0.8           # staleness_score 高于此值标记审查
COLD_RETENTION_DAYS = 90
STRENGTH_ENDANGERED = 0.15          # R(t) 低于此值为濒危
MIN_FOR_REFLECTION = 30             # 触发反思的最少情景记忆数
MAX_REVISIONS = 20                  # 单次巩固最多修正数

# ── 注意力门控 ──
EXPLICIT_MARK_IMPORTANCE = 0.9
GOAL_RELEVANCE_PASS = 0.5           # goal_relevance > 此值通过门控
HIGH_EMOTION_DEFAULT = 0.85         # 高情绪信号默认 emotion_weight

# ── 检索 ──
DEFAULT_TOP_K = 10
MAX_SEARCH_RESULTS = 50

# 高情绪信号关键词（中英文）
EMOTIONAL_KEYWORDS_FAILURE = ["失败", "错误", "崩溃", "bug", "error", "fail", "异常", "报错"]
EMOTIONAL_KEYWORDS_BREAKTHROUGH = ["解决", "突破", "终于", "找到", "fixed", "solved", "搞定"]
EMOTIONAL_KEYWORDS_CORRECTION = ["以后", "改用", "替代", "instead", "replace", "不要再用"]
EMOTIONAL_KEYWORDS_CONFLICT = ["冲突", "矛盾", "contradict", "不一致"]

# ── 服务器 ──
HOST = "127.0.0.1"
PORT = 8765
