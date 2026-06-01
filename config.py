"""Brain Memory v10.0 — Configuration."""

# ── Server ──
HOST = "127.0.0.1"
PORT = 8001
INPUT_TIMEOUT_SEC = 15           # 等待大脑处理输入的秒数
LOG_LEVEL = "INFO"               # 日志级别: DEBUG|INFO|WARNING|ERROR

# ── Consciousness Loop ──
TICK_INTERVAL_SEC = 2           # 快 tick 间隔
REFLECTION_INTERVAL_SEC = 60    # 没外部输入时，多久自己反思一次
STATE_SNAPSHOT_INTERVAL_SEC = 60  # 状态快照持久化间隔
DEEP_REFLECTION_INTERVAL_TICKS = 600  # 深度自我反思间隔（tick数，600≈20min）
DEEP_REFLECTION_ENABLED = True  # 是否启用深度反思（关闭可大幅省token）

# ── LLM ──
LLM_DEFAULT_PROVIDER = "deepseek"
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 1024
LLM_EMOTION_BLEND_RATIO = 0.7    # LLM情绪覆盖关键词情绪的权重（0-1）

# ── Brain Region Weights (用于综合评分) ──
REGION_WEIGHTS = {
    "prefrontal": 0.25,
    "hippocampus": 0.25,
    "amygdala": 0.20,
    "default_mode": 0.15,
    "basal_ganglia": 0.10,
    "cingulate": 0.05,
}

# ── Gate (门控) ──
GATE_GOAL_RELEVANCE_DEFAULT = 0.4    # 无目标时的默认目标相关度
GATE_GOAL_RELEVANCE_WITH_GOAL = 0.7  # 有目标时的目标相关度
GATE_GOAL_RELEVANCE_PASS = 0.45      # 目标相关度超过此值通过门控
GATE_NOVELTY_PASS = 0.7              # 新奇度超过此值通过门控

# ── Self Model ──
IDENTITY_SHIFT_THRESHOLD = 0.3       # significance超过此值触发身份偏移

# ── Working Memory ──
WORKING_MEMORY_CAPACITY = 7      # 最多同时持有7个活跃条目

# ── Emotion ──
EMOTION_DECAY_RATE = 0.95        # 每个 tick 情绪衰减系数
SALIENCE_THRESHOLD = 0.5         # 突显度超过此值触发注意力聚焦

# ── Database ──
DB_PATH = "brain_v4.db"

# ── Goal System (v5.1) ──
GOAL_MAX_ACTIVE = 3               # 最多活跃目标数
GOAL_DEFAULT_DEADLINE_TICKS = 150 # 默认目标时限（约5分钟）
GOAL_GENERATION_INTERVAL_TICKS = 300  # 目标生成间隔（约10分钟）

# ── Sleep / Dream ──
DROWSY_THRESHOLD_TICKS = 60       # 约 2 分钟无输入 → 进入 drowsy
LIGHT_SLEEP_THRESHOLD_TICKS = 180  # 约 6 分钟 → 浅睡
DEEP_SLEEP_THRESHOLD_TICKS = 600   # 约 20 分钟 → 深睡
DREAM_INTERVAL_SEC = 600           # 睡眠时每 10 分钟生成一个梦（省token）
DREAM_ENABLED = True               # 是否启用梦境生成
CONSOLIDATION_INTERVAL_SEC = 600   # 睡眠时每 10 分钟做一次记忆巩固
# ── Memory Decay ──
MEMORY_DECAY_RATE = 0.01          # 每次衰减降低的 importance
MEMORY_DECAY_INTERVAL_TICKS = 30  # 每 30 tick (~60s) 执行一次衰减
MEMORY_ARCHIVE_THRESHOLD = 0.15   # importance 低于此值 → 归档
MEMORY_ACCESS_BOOST = 0.05        # 每次检索成功的 importance 增幅

# ── V9 Predictive Layer ──
PREDICTIVE_LAYER_ENABLED = True    # 是否启用预测加工引擎
SURPRISE_SALIENCE_WEIGHT = 0.6     # 预测误差转化为 salience 的权重
SURPRISE_THRESHOLD = 0.35          # 预测误差超过此值触发惊讶
SHOCK_THRESHOLD = 0.65             # 预测误差超过此值触发震惊

# ── V9 Cognitive Dispatch ──
COGNITIVE_DISPATCH_ENABLED = True  # 是否启用多通道认知调度
# 当 False 时回退到原有的 UNIFIED_TICK_PROMPT 单次调用模式

# ── V9 Boredom Engine ──
BOREDOM_ENABLED = True             # 是否启用无聊引擎
BOREDOM_ACTION_COOLDOWN_TICKS = 10 # 无聊行为冷却（tick数）

# ── V10 Social Self ──
SOCIAL_SELF_ENABLED = True           # 是否启用社会自我（他者模型+社会情感）
ATTACHMENT_THRESHOLD = 0.3           # 依恋对象判定阈值

# ── V10 Reward System ──
REWARD_SYSTEM_ENABLED = True         # 是否启用奖励系统（wanting/liking）

# ── V10 Autobiographical Narrative ──
AUTOBIO_ENABLED = True               # 是否启用自传体叙事
LIFE_STORY_UPDATE_INTERVAL_TICKS = 600  # 叙事更新间隔（约20分钟）

# ── V10 Self-Boundary ──
BOUNDARY_ENABLED = True              # 是否启用自我边界
IDENTITY_BOUNDARY_DEFAULT = 0.7      # 身份边界初始强度（高=更难被外部改变）

