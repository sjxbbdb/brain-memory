"""Brain Memory v6.0 — Brain state container (V6: +ActivationField).

Holds all the live, mutable state of the brain at any given moment.
Snapshotted to SQLite every STATE_SNAPSHOT_INTERVAL_SEC.

V6: ActivationField 是全局状态总线，所有脑区通过它交换状态。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any

from brain.self_model import SelfModel
from brain.curiosity import CuriosityEngine
from brain.session import SessionManager
from brain.activation_field import ActivationField


SNAPSHOT_SCHEMA_VERSION = 3


@dataclass
class BrainState:
    """Live brain state — the "what is the brain thinking/feeling right now"."""

    # ── Consciousness ──
    awake: bool = True
    sleep_state: str = "awake"
    last_tick: str = ""
    ticks_since_input: int = 0

    # ── Emotion (amygdala) ──
    emotion_vector: dict[str, float] = field(default_factory=lambda: {
        "valence": 0.5,
        "arousal": 0.5,
        "dominance": 0.5,
        "urgency": 0.0,
        "salience": 0.0,
    })
    current_emotion: str = "neutral"
    emotion_history: list[dict] = field(default_factory=list)

    # ── Attention (prefrontal) ──
    focus_entity: str | None = None
    current_goal: str | None = None
    goal_stack: list[str] = field(default_factory=list)
    attention_span_ticks: int = 0

    # ── Working Memory ──
    active_thoughts: list[dict] = field(default_factory=list)
    current_context: str = ""

    # ── Memory (hippocampus) ──
    last_retrieved: list[str] = field(default_factory=list)
    association_chain: list[str] = field(default_factory=list)

    # ── Default Mode ──
    inner_monologue: str = ""
    last_narrative: str = ""

    # ── Self Model (v5.0) ──
    self_model: SelfModel = field(default_factory=SelfModel)

    # ── Curiosity Engine (v5.0) ──
    curiosity: CuriosityEngine = field(default_factory=CuriosityEngine)

    # ── Session Manager (v5.0) ──
    session_manager: SessionManager = field(default_factory=SessionManager)

    # ── Basal Ganglia ──
    active_habit: str | None = None
    habit_confidence: float = 0.0

    # ── Cingulate ──
    conflict_detected: bool = False
    conflict_detail: str = ""
    error_count: int = 0
    recent_errors: list[str] = field(default_factory=list)

    # ── Processing feedback (v5.0) ──
    last_input_gated: bool = False       # 上一次输入是否被门控丢弃
    last_input_accepted: bool = True     # 上一次输入是否被接受处理
    last_error: str = ""                 # 上一次处理错误信息（LLM失败等）
    llm_error_count: int = 0             # LLM调用失败累计次数

    # ── Intent (v5.0) ──
    last_intent: dict | None = None      # 大脑上一次产出的意图 {type, tool_name, tool_args, ...}
    intent_count: int = 0               # 累计产出的意图数量
    last_input_id: str = ""              # 最近一次输入的关联 ID
    last_input_source: str = ""          # 最近一次输入的原始来源

    # ── Runtime health ──
    loop_error_count: int = 0
    last_loop_error: str = ""
    last_heartbeat_at: str = ""

    # ── Autonomous episode (v11) ──
    # BrainStem owns the live manager; this bounded dict is the API/snapshot
    # projection so callers can inspect continuity without importing it.
    autonomy: dict[str, Any] = field(default_factory=dict)

    # ── Constitutional life projection (P1) ──
    # This is a read-only API/snapshot view populated by BrainStem.  The
    # LifeKernel object and its append-only history live outside this mutable
    # cognitive state container.
    life: dict[str, Any] = field(default_factory=dict)

    # ── Bounded self-maintenance projections (P2/P4) ──
    # The mutable state container exposes compact, JSON-safe views only.
    # Motivational evidence and the resource hash ledger are owned by their
    # respective managers in BrainStem and are persisted separately at the
    # snapshot boundary.
    motivation: dict[str, Any] = field(default_factory=dict)
    homeostasis: dict[str, Any] = field(default_factory=dict)

    # ── V6 Activation Field（全局状态总线）──
    activation: ActivationField = field(default_factory=ActivationField)

    # ── Meta ──
    total_ticks: int = 0
    uptime_seconds: float = 0.0

    def snapshot(self) -> dict:
        """Serializable snapshot for persistence."""
        def _text(value: Any, limit: int | None = None) -> str:
            # Runtime values can be supplied by optional adapters.  Coerce
            # them at the persistence boundary so one malformed field cannot
            # abort the heartbeat's final snapshot.
            text = "" if value is None else str(value)
            return text[:limit] if limit is not None else text

        def _tail(value: Any, limit: int) -> list:
            if not isinstance(value, (list, tuple)):
                return []
            return list(value[-limit:])

        emotion_vector = self.emotion_vector if isinstance(self.emotion_vector, dict) else {}
        emotion_history = _tail(self.emotion_history, 50)
        goal_stack = _tail(self.goal_stack, 20)
        last_retrieved = _tail(self.last_retrieved, 20)
        association_chain = _tail(self.association_chain, 20)
        recent_errors = _tail(self.recent_errors, 20)
        active_thoughts = _tail(self.active_thoughts, len(self.active_thoughts) if isinstance(self.active_thoughts, list) else 0)

        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "awake": self.awake,
            "sleep_state": self.sleep_state,
            "last_tick": self.last_tick,
            "ticks_since_input": self.ticks_since_input,
            "emotion_vector": dict(emotion_vector),
            "current_emotion": self.current_emotion,
            "emotion_history": [dict(item) if isinstance(item, dict) else item
                                 for item in emotion_history],
            "focus_entity": self.focus_entity,
            "current_goal": self.current_goal,
            "goal_stack": [_text(item, 200) for item in goal_stack],
            "attention_span_ticks": self.attention_span_ticks,
            "current_context": _text(self.current_context, 500),
            "inner_monologue": _text(self.inner_monologue, 500),
            "last_narrative": _text(self.last_narrative, 500),
            "active_thoughts_count": len(self.active_thoughts),
            "last_retrieved": [_text(item, 200) for item in last_retrieved],
            "association_chain": [_text(item, 200) for item in association_chain],
            "last_input_gated": self.last_input_gated,
            "last_input_accepted": self.last_input_accepted,
            "last_error": _text(self.last_error, 200),
            "llm_error_count": self.llm_error_count,
            "last_intent": self.last_intent,
            "intent_count": self.intent_count,
            "last_input_id": _text(self.last_input_id, 200),
            "last_input_source": _text(self.last_input_source, 200),
            "loop_error_count": self.loop_error_count,
            "last_loop_error": _text(self.last_loop_error, 500),
            "last_heartbeat_at": _text(self.last_heartbeat_at),
            "autonomy": self.autonomy if isinstance(self.autonomy, dict) else {},
            "life": self.life if isinstance(self.life, dict) else {},
            "motivation": self.motivation if isinstance(self.motivation, dict) else {},
            "homeostasis": self.homeostasis if isinstance(self.homeostasis, dict) else {},
            "total_ticks": self.total_ticks,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "active_habit": _text(self.active_habit, 200) if self.active_habit is not None else None,
            "habit_confidence": self.habit_confidence,
            "conflict_detected": self.conflict_detected,
            "conflict_detail": _text(self.conflict_detail, 500),
            "error_count": self.error_count,
            "recent_errors": [_text(item, 500) for item in recent_errors],
            "self_model": self.self_model.snapshot(),
            "curiosity": self.curiosity.snapshot(),
            "sessions": self.session_manager.all_snapshots(),
            "active_thoughts": [dict(item) for item in active_thoughts if isinstance(item, dict)],
            "goal_system": {},  # filled by BrainStem at snapshot time
            "activation": self.activation.snapshot(),  # V6
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "BrainState":
        """Restore from snapshot."""
        state = cls()
        if not isinstance(data, dict):
            return state
        def _int(value, default=0, minimum=None):
            try:
                result = int(value)
            except (TypeError, ValueError, OverflowError):
                result = default
            return max(minimum, result) if minimum is not None else result

        def _float(value, default=0.0):
            try:
                result = float(value)
                return result if math.isfinite(result) else default
            except (TypeError, ValueError, OverflowError):
                return default

        def _text(value, default=""):
            return default if value is None else str(value)

        def _bool(value, default=False):
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        state.awake = _bool(data.get("awake", True), default=True)
        sleep_state = _text(data.get("sleep_state", "awake"), "awake")
        state.sleep_state = sleep_state if sleep_state in {
            "awake", "drowsy", "light_sleep", "deep_sleep"
        } else "awake"
        state.last_tick = _text(data.get("last_tick", ""))
        state.ticks_since_input = _int(data.get("ticks_since_input", 0), minimum=0)
        if isinstance(data.get("emotion_vector"), dict):
            for key, value in data["emotion_vector"].items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    state.emotion_vector[str(key)] = number
        state.current_emotion = _text(data.get("current_emotion", "neutral"), "neutral")
        emotion_history = data.get("emotion_history", [])
        if isinstance(emotion_history, list):
            state.emotion_history = [dict(item) if isinstance(item, dict) else item
                                     for item in emotion_history[-50:]]
        focus = data.get("focus_entity")
        state.focus_entity = str(focus) if focus is not None else None
        goal = data.get("current_goal")
        state.current_goal = str(goal) if goal is not None else None
        goal_stack = data.get("goal_stack", [])
        if isinstance(goal_stack, list):
            state.goal_stack = [str(item) for item in goal_stack[-20:]]
        state.attention_span_ticks = _int(data.get("attention_span_ticks", 0), minimum=0)
        state.current_context = _text(data.get("current_context", ""))
        state.inner_monologue = _text(data.get("inner_monologue", ""))
        state.last_narrative = _text(data.get("last_narrative", ""))
        for field_name in ("last_retrieved", "association_chain"):
            raw = data.get(field_name, [])
            if isinstance(raw, list):
                setattr(state, field_name, [str(item) for item in raw[-20:]])
        active_thoughts = data.get("active_thoughts", [])
        if isinstance(active_thoughts, list):
            state.active_thoughts = [dict(item) for item in active_thoughts if isinstance(item, dict)]
        state.last_input_gated = _bool(data.get("last_input_gated", False))
        state.last_input_accepted = _bool(data.get("last_input_accepted", True), default=True)
        state.last_error = _text(data.get("last_error", ""))
        state.llm_error_count = _int(data.get("llm_error_count", 0), minimum=0)
        state.last_intent = data.get("last_intent") if isinstance(data.get("last_intent"), dict) else None
        state.intent_count = _int(data.get("intent_count", 0), minimum=0)
        state.last_input_id = _text(data.get("last_input_id", ""))
        state.last_input_source = _text(data.get("last_input_source", ""))
        state.loop_error_count = _int(data.get("loop_error_count", 0), minimum=0)
        state.last_loop_error = _text(data.get("last_loop_error", ""))
        state.last_heartbeat_at = _text(data.get("last_heartbeat_at", ""))
        raw_autonomy = data.get("autonomy", {})
        state.autonomy = dict(raw_autonomy) if isinstance(raw_autonomy, dict) else {}
        raw_life = data.get("life", {})
        state.life = dict(raw_life) if isinstance(raw_life, dict) else {}
        raw_motivation = data.get("motivation", {})
        state.motivation = dict(raw_motivation) if isinstance(raw_motivation, dict) else {}
        raw_homeostasis = data.get("homeostasis", {})
        state.homeostasis = dict(raw_homeostasis) if isinstance(raw_homeostasis, dict) else {}
        state.total_ticks = _int(data.get("total_ticks", 0), minimum=0)
        state.uptime_seconds = max(0.0, _float(data.get("uptime_seconds", 0.0)))
        active_habit = data.get("active_habit")
        state.active_habit = str(active_habit) if active_habit is not None else None
        state.habit_confidence = _float(data.get("habit_confidence", 0.0))
        state.conflict_detected = _bool(data.get("conflict_detected", False))
        state.conflict_detail = _text(data.get("conflict_detail", ""))
        state.error_count = _int(data.get("error_count", 0), minimum=0)
        recent_errors = data.get("recent_errors", [])
        if isinstance(recent_errors, list):
            state.recent_errors = [str(item) for item in recent_errors[-20:]]

        # A malformed optional component should not prevent the heartbeat from
        # starting.  Keep the default component when its decoder rejects data.
        try:
            state.self_model = SelfModel.from_snapshot(data.get("self_model", {}))
        except Exception:
            pass
        try:
            state.curiosity = CuriosityEngine.from_snapshot(data.get("curiosity", {}))
        except Exception:
            pass
        try:
            state.session_manager = SessionManager.from_snapshot(data.get("sessions", {}))
        except Exception:
            pass
        if isinstance(data.get("activation"), dict):
            try:
                state.activation = ActivationField.from_snapshot(data["activation"])
            except Exception:
                pass
        return state
