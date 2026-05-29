"""Session Manager — per-source isolation for multi-agent use.

Each source (hermes, codex, dashboard, etc.) gets its own:
- Working memory slots
- Emotional state
- Attention focus
- Inner monologue

Shared across all sessions:
- Long-term memory (hippocampus)
- Self-model
- Curiosity engine
"""

from dataclasses import dataclass, field
from brain.working_memory import WorkingMemory


@dataclass
class SessionState:
    """Per-source session state."""
    source: str
    working_memory: WorkingMemory = field(default_factory=WorkingMemory)
    emotion_vector: dict = field(default_factory=lambda: {
        "valence": 0.5, "arousal": 0.5, "dominance": 0.5,
        "urgency": 0.0, "salience": 0.0,
    })
    current_emotion: str = "neutral"
    focus_entity: str | None = None
    inner_monologue: str = ""
    current_context: str = ""
    last_active: str = ""


class SessionManager:
    """Manages per-source session isolation."""

    def __init__(self):
        self.sessions: dict[str, SessionState] = {}
        self.max_sessions: int = 20
        self.default_source: str = "default"

    def get(self, source: str | None) -> SessionState:
        """Get or create a session for the given source."""
        key = source or self.default_source
        if key not in self.sessions:
            # Evict oldest if at capacity
            if len(self.sessions) >= self.max_sessions:
                oldest = min(
                    self.sessions.keys(),
                    key=lambda k: self.sessions[k].last_active,
                )
                del self.sessions[oldest]
            self.sessions[key] = SessionState(source=key)
        return self.sessions[key]

    def get_all_sources(self) -> list[str]:
        return list(self.sessions.keys())

    def get_session_count(self) -> int:
        return len(self.sessions)

    def snapshot(self, source: str | None = None) -> dict:
        """Snapshot a specific session or return all."""
        from datetime import datetime, timezone

        if source:
            s = self.get(source)
            return {
                "source": s.source,
                "emotion_vector": dict(s.emotion_vector),
                "current_emotion": s.current_emotion,
                "focus_entity": s.focus_entity,
                "inner_monologue": s.inner_monologue[:200],
                "current_context": s.current_context[:300],
                "last_active": s.last_active,
            }

        return {
            "sources": {
                k: {
                    "emotion": v.current_emotion,
                    "focus": v.focus_entity,
                    "monologue": v.inner_monologue[:100],
                    "last_active": v.last_active,
                }
                for k, v in self.sessions.items()
            },
            "active_count": len(self.sessions),
        }

    def all_snapshots(self) -> dict:
        """Snapshot all sessions."""
        from datetime import datetime, timezone
        return {
            "active_sources": list(self.sessions.keys()),
            "count": len(self.sessions),
            "sessions": {
                k: {
                    "emotion": v.current_emotion,
                    "emotion_vector": dict(v.emotion_vector),
                    "focus": v.focus_entity,
                    "monologue": v.inner_monologue[:100],
                    "context": v.current_context[:200],
                    "last_active": v.last_active,
                }
                for k, v in self.sessions.items()
            },
        }
