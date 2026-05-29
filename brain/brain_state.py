"""Brain Memory v4.0 — Brain state container (v4.1: +self_model).

Holds all the live, mutable state of the brain at any given moment.
Snapshotted to SQLite every STATE_SNAPSHOT_INTERVAL_SEC.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from brain.self_model import SelfModel
from brain.curiosity import CuriosityEngine
from brain.session import SessionManager, SessionState


@dataclass
class BrainState:
    """Live brain state — the "what is the brain thinking/feeling right now"."""

    # ── Consciousness ──
    awake: bool = True
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

    # ── Self Model (v4.1) ──
    self_model: SelfModel = field(default_factory=SelfModel)

    # ── Curiosity Engine (v4.1) ──
    curiosity: CuriosityEngine = field(default_factory=CuriosityEngine)

    # ── Session Manager (v4.1) ──
    session_manager: SessionManager = field(default_factory=SessionManager)

    # ── Basal Ganglia ──
    active_habit: str | None = None
    habit_confidence: float = 0.0

    # ── Cingulate ──
    conflict_detected: bool = False
    conflict_detail: str = ""
    error_count: int = 0
    recent_errors: list[str] = field(default_factory=list)

    # ── Meta ──
    total_ticks: int = 0
    uptime_seconds: float = 0.0

    def snapshot(self) -> dict:
        """Serializable snapshot for persistence."""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "awake": self.awake,
            "emotion_vector": dict(self.emotion_vector),
            "current_emotion": self.current_emotion,
            "focus_entity": self.focus_entity,
            "current_goal": self.current_goal,
            "current_context": self.current_context[:500],
            "inner_monologue": self.inner_monologue[:500],
            "active_thoughts_count": len(self.active_thoughts),
            "total_ticks": self.total_ticks,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "self_model": self.self_model.snapshot(),
            "curiosity": self.curiosity.snapshot(),
            "sessions": self.session_manager.all_snapshots(),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "BrainState":
        """Restore from snapshot."""
        state = cls()
        state.awake = data.get("awake", True)
        if data.get("emotion_vector"):
            state.emotion_vector.update(data["emotion_vector"])
        state.current_emotion = data.get("current_emotion", "neutral")
        state.focus_entity = data.get("focus_entity")
        state.current_goal = data.get("current_goal")
        state.current_context = data.get("current_context", "")
        state.inner_monologue = data.get("inner_monologue", "")
        state.total_ticks = data.get("total_ticks", 0)
        state.uptime_seconds = data.get("uptime_seconds", 0.0)
        state.self_model = SelfModel.from_snapshot(data.get("self_model", {}))
        state.curiosity = CuriosityEngine.from_snapshot(data.get("curiosity", {}))
        # Sessions are ephemeral, not restored from snapshot
        return state
