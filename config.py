"""Brain Memory v10.0 — Configuration."""

import os


def _env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read a bounded integer override without making startup fragile."""
    try:
        result = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        result = default
    if minimum is not None and result < minimum:
        result = default
    if maximum is not None and result > maximum:
        result = default
    return result


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

# ── Server ──
HOST = os.getenv("BRAIN_MEMORY_HOST", "127.0.0.1") or "127.0.0.1"
PORT = _env_int("BRAIN_MEMORY_PORT", 8001, minimum=1, maximum=65535)
INPUT_TIMEOUT_SEC = 15           # 等待大脑处理输入的秒数
# Bound external/optional cognitive work inside one heartbeat.  The caller
# may still receive a ``pending`` response after INPUT_TIMEOUT_SEC, but the
# heartbeat itself must regain control and continue maintenance promptly.
COGNITIVE_TIMEOUT_SEC = _env_int(
    "BRAIN_MEMORY_COGNITIVE_TIMEOUT_SEC", 10, minimum=1, maximum=120
)
LOG_LEVEL = "INFO"               # 日志级别: DEBUG|INFO|WARNING|ERROR

# ── Consciousness Loop ──
TICK_INTERVAL_SEC = 2           # 快 tick 间隔
REFLECTION_INTERVAL_SEC = 60    # 没外部输入时，多久自己反思一次
STATE_SNAPSHOT_INTERVAL_SEC = 60  # 状态快照持久化间隔
DEEP_REFLECTION_INTERVAL_TICKS = 600  # 深度自我反思间隔（tick数，600≈20min）
DEEP_REFLECTION_ENABLED = True  # 是否启用深度反思（关闭可大幅省token）

# ── Autonomous Episode ──
# The episode layer turns existing drives/goals/tools into one bounded,
# observable action cycle.  It is deliberately conservative by default:
# one episode at a time and a finite wait before safe failure.
AUTONOMY_ENABLED = _env_bool("BRAIN_MEMORY_AUTONOMY_ENABLED", True)
AUTONOMY_GOAL_INTERVAL_TICKS = _env_int(
    "BRAIN_MEMORY_AUTONOMY_GOAL_INTERVAL_TICKS", 30, minimum=1, maximum=100000
)       # ~60s at the default 2s heartbeat
AUTONOMY_MAX_EPISODE_TICKS = _env_int(
    "BRAIN_MEMORY_AUTONOMY_MAX_EPISODE_TICKS", 180, minimum=10, maximum=100000
)        # ~6 minutes before safe timeout

# Long-term task policy.  The scheduler keeps one execution lane while
# retaining a bounded queue for maintenance, user, and exploration work.
TASK_QUEUE_LIMIT = _env_int(
    "BRAIN_MEMORY_TASK_QUEUE_LIMIT", 12, minimum=3, maximum=100
)
TASK_MAINTENANCE_BUDGET_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_MAINTENANCE_BUDGET_TICKS", 60, minimum=1, maximum=100000
)
TASK_USER_BUDGET_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_USER_BUDGET_TICKS", 120, minimum=1, maximum=100000
)
TASK_EXPLORATION_BUDGET_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_EXPLORATION_BUDGET_TICKS", 90, minimum=1, maximum=100000
)
TASK_MAINTENANCE_DEADLINE_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_MAINTENANCE_DEADLINE_TICKS", 180, minimum=1, maximum=100000
)
TASK_USER_DEADLINE_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_USER_DEADLINE_TICKS", 360, minimum=1, maximum=100000
)
TASK_EXPLORATION_DEADLINE_TICKS = _env_int(
    "BRAIN_MEMORY_TASK_EXPLORATION_DEADLINE_TICKS", 240, minimum=1, maximum=100000
)

# V13 task execution / verification loop.  The limits are deliberately
# conservative so a long-running process cannot grow an unbounded plan or
# replay an unverified action after restart.  ``REQUIRE_VERIFIED_COMPLETION``
# is enabled by default: a returning tool call is not, by itself, proof that
# a goal was achieved.
TASK_EXECUTION_ENABLED = _env_bool("BRAIN_MEMORY_TASK_EXECUTION_ENABLED", True)
TASK_EXECUTION_MAX_PLANS = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_PLANS", 32, minimum=1, maximum=1000
)
TASK_EXECUTION_MAX_STEPS = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_STEPS", 8, minimum=1, maximum=128
)
TASK_EXECUTION_MAX_DEPTH = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_DEPTH", 3, minimum=0, maximum=16
)
TASK_EXECUTION_MAX_RETRIES = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_RETRIES", 2, minimum=0, maximum=10
)
TASK_EXECUTION_MAX_ACTIONS = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_ACTIONS", 128, minimum=1, maximum=5000
)
TASK_EXECUTION_MAX_OBSERVATIONS = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_OBSERVATIONS", 128, minimum=1, maximum=5000
)
TASK_EXECUTION_MAX_OUTCOMES = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_OUTCOMES", 128, minimum=1, maximum=5000
)
TASK_EXECUTION_MAX_EVENTS = _env_int(
    "BRAIN_MEMORY_TASK_EXECUTION_MAX_EVENTS", 256, minimum=1, maximum=10000
)
TASK_EXECUTION_REQUIRE_VERIFIED_COMPLETION = _env_bool(
    "BRAIN_MEMORY_TASK_EXECUTION_REQUIRE_VERIFIED_COMPLETION", True
)
TASK_EXECUTION_AUTO_REPLAN = _env_bool(
    "BRAIN_MEMORY_TASK_EXECUTION_AUTO_REPLAN", False
)

# The API process can host the read-only execution boundary alongside the
# brain.  Writes stay disabled unless an operator explicitly opts in.
AGENT_BRIDGE_ENABLED = _env_bool("BRAIN_MEMORY_AGENT_BRIDGE_ENABLED", True)
AGENT_BRIDGE_ALLOW_WRITE_TOOLS = _env_bool(
    "BRAIN_MEMORY_AGENT_BRIDGE_ALLOW_WRITE_TOOLS", False
)

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
DB_PATH = os.getenv("BRAIN_MEMORY_DB_PATH", "brain_v4.db") or "brain_v4.db"

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

