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
import math
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
                "working_memory": s.working_memory.snapshot(),
            }

        return {
            "sources": {
                k: {
                    "emotion": v.current_emotion,
                    "emotion_vector": dict(v.emotion_vector),
                    "focus": v.focus_entity,
                    "monologue": v.inner_monologue[:100],
                    "context": v.current_context[:200],
                    "last_active": v.last_active,
                    "working_memory": v.working_memory.snapshot(),
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
            "max_sessions": self.max_sessions,
            "default_source": self.default_source,
            "sessions": {
                k: {
                    "emotion": v.current_emotion,
                    "emotion_vector": dict(v.emotion_vector),
                    "focus": v.focus_entity,
                    "monologue": v.inner_monologue[:100],
                    "context": v.current_context[:200],
                    "last_active": v.last_active,
                    "working_memory": v.working_memory.snapshot(),
                }
                for k, v in self.sessions.items()
            },
        }

    @classmethod
    def from_snapshot(cls, data: dict | None) -> "SessionManager":
        """Restore per-source context from a full or legacy snapshot."""
        manager = cls()
        if not isinstance(data, dict):
            return manager

        try:
            manager.max_sessions = min(
                1000,
                max(1, int(data.get("max_sessions", manager.max_sessions))),
            )
        except (TypeError, ValueError):
            pass
        if data.get("default_source"):
            manager.default_source = str(data["default_source"])

        sessions = data.get("sessions", data.get("sources", {}))
        if not isinstance(sessions, dict):
            return manager

        for source, raw in list(sessions.items())[: manager.max_sessions]:
            if not isinstance(raw, dict):
                continue
            source_id = str(source)[:200] or manager.default_source
            session = SessionState(source=source_id)
            session.current_emotion = str(raw.get("emotion", raw.get("current_emotion", "neutral")))
            vector = raw.get("emotion_vector", {})
            if isinstance(vector, dict):
                for key, value in vector.items():
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(number):
                        session.emotion_vector[str(key)] = number
            focus = raw.get("focus", raw.get("focus_entity"))
            session.focus_entity = str(focus)[:200] if focus is not None else None
            session.inner_monologue = str(raw.get("monologue", raw.get("inner_monologue", "")))[:500]
            session.current_context = str(raw.get("context", raw.get("current_context", "")))[:1000]
            session.last_active = str(raw.get("last_active", ""))
            raw_wm = raw.get("working_memory", {})
            if not isinstance(raw_wm, dict) or not raw_wm.get("items"):
                # Legacy snapshots stored only the compact context string.
                raw_wm = {
                    "items": raw_wm.get("items", []) if isinstance(raw_wm, dict) else [],
                    "context_text": session.current_context,
                }
            session.working_memory = WorkingMemory.from_snapshot(raw_wm)
            manager.sessions[source_id] = session

        return manager
