"""Brain Memory v4.0 — Configuration."""

# ── Server ──
HOST = "127.0.0.1"
PORT = 8001

# ── Consciousness Loop ──
TICK_INTERVAL_SEC = 2           # 快 tick 间隔
REFLECTION_INTERVAL_SEC = 60    # 没外部输入时，多久自己反思一次
STATE_SNAPSHOT_INTERVAL_SEC = 60  # 状态快照持久化间隔

# ── LLM ──
LLM_DEFAULT_PROVIDER = "deepseek"
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 1024

# ── Brain Region Weights (用于综合评分) ──
REGION_WEIGHTS = {
    "prefrontal": 0.25,
    "hippocampus": 0.25,
    "amygdala": 0.20,
    "default_mode": 0.15,
    "basal_ganglia": 0.10,
    "cingulate": 0.05,
}

# ── Working Memory ──
WORKING_MEMORY_CAPACITY = 7      # 最多同时持有7个活跃条目

# ── Emotion ──
EMOTION_DECAY_RATE = 0.95        # 每个 tick 情绪衰减系数
SALIENCE_THRESHOLD = 0.5         # 突显度超过此值触发注意力聚焦

# ── Database ──
DB_PATH = "brain_v4.db"

# ── Sleep / Dream ──
DROWSY_THRESHOLD_TICKS = 60       # 约 2 分钟无输入 → 进入 drowsy
LIGHT_SLEEP_THRESHOLD_TICKS = 180  # 约 6 分钟 → 浅睡
DEEP_SLEEP_THRESHOLD_TICKS = 600   # 约 20 分钟 → 深睡
DREAM_INTERVAL_SEC = 120           # 睡眠时每 2 分钟生成一个梦
CONSOLIDATION_INTERVAL_SEC = 300   # 睡眠时每 5 分钟做一次记忆巩固
# ── Memory Decay ──
MEMORY_DECAY_RATE = 0.01          # 每次衰减降低的 importance
MEMORY_DECAY_INTERVAL_TICKS = 30  # 每 30 tick (~60s) 执行一次衰减
MEMORY_ARCHIVE_THRESHOLD = 0.15   # importance 低于此值 → 归档
MEMORY_ACCESS_BOOST = 0.05        # 每次检索成功的 importance 增幅

