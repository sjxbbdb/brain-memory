"""Brain — v4.0 core agent. The brain memory system as a conscious agent.

Usage:
    brain = Brain()
    await brain.wake_up()
    
    # Send input
    response = await brain.process_input("GPIO配置失败了...", goal="修复GPIO")
    
    # Get current state
    state = await brain.get_state()
    
    # Listen to output feed
    event = await brain.next_output()
    
    # Shutdown
    await brain.sleep()
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from brain.brain_stem import BrainStem
from storage.database import init_db, StateStore, MemoryStore

logger = logging.getLogger("brain-v4.core")


class Brain:
    """Brain Memory v4.0 — a conscious memory agent."""

    def __init__(self, db_path: str = "brain_v4.db"):
        self.state_store = StateStore(db_path)
        self.memory_store = MemoryStore(db_path)
        self.brain_stem = BrainStem(
            state_store=self.state_store,
            memory_store=self.memory_store,
        )
        self._wake = False
        self._ws_clients: set = set()  # WebSocket clients

    # ── Lifecycle ──

    async def wake_up(self):
        """Wake the brain. Starts consciousness loop."""
        init_db()
        await self.brain_stem.start()
        self._wake = True
        logger.info("brain: awake")

    async def sleep(self):
        """Put the brain to sleep. Stops consciousness loop."""
        await self.brain_stem.stop()
        self._wake = False
        logger.info("brain: asleep")

    async def get_identity_memories(self, limit: int = 20) -> list[dict]:
        """Get memories that shaped the identity."""
        return self.memory_store.get_identity_memories(limit)

    @property
    def is_awake(self) -> bool:
        return self._wake

    # ── Input ──

    async def process_input(
        self,
        text: str,
        source: str = "external",
        goal: str | None = None,
    ) -> dict:
        """Process input and return context for the calling agent.

        This is the main entry point for other agents to talk to the brain.
        Uses a completion event to wait for the brain to finish processing.
        """
        # Create a one-shot event for this input
        done_event = asyncio.Event()

        # Wrap the original queue to signal when consumed
        async def _wrapped_input():
            await self.brain_stem._pending_input.put({
                "text": text, "source": source, "goal": goal,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        await _wrapped_input()

        # Wait for the brain to process the input (via event signal from brain_stem)
        try:
            await asyncio.wait_for(self.brain_stem._input_processed.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            pass  # timeout, return whatever state we have

        # Return current context for the calling agent
        sm = self.brain_stem.state.self_model
        return {
            "accepted": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "focus": self.brain_stem.state.focus_entity,
            "emotion": self.brain_stem.state.current_emotion,
            "inner_monologue": self.brain_stem.state.inner_monologue[:200],
            "working_memory": self.brain_stem.state.current_context[:300],
            "memories": {
                "total": self.memory_store.count(),
                "identity_forming": len(self.memory_store.get_identity_memories(20)),
            },
            "self": {
                "identity": sm.identity_anchor[:200],
                "traits": sm.identity_traits,
                "top_drives": [
                    {"name": d["name"], "label": d["label"], "weight": d["weight"]}
                    for d in sm.get_top_drives(3)
                ],
                "mood": sm.mood_tendency,
                "version": sm.identity_version,
                "experiences": sm.total_experiences,
                "last_reflection": sm.last_reflection[:100],
            },
            "session": {
                "source": source,
                "active_sessions": self.brain_stem.state.session_manager.get_session_count(),
            },
        }

    # ── Output ──

    async def next_output(self, timeout: float = 1.0) -> dict | None:
        """Get next output event from the brain's output feed."""
        try:
            return await asyncio.wait_for(self.brain_stem._output_feed.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ── State ──

    async def get_state(self) -> dict:
        """Get full brain state for external inspection."""
        return self.brain_stem.state.snapshot()

    async def get_inner_monologue(self) -> str:
        """Get the brain's current inner monologue."""
        return self.brain_stem.state.inner_monologue or ""

    # ── WebSocket ──

    def register_ws(self, client):
        """Register a WebSocket client for state pushes."""
        self._ws_clients.add(client)

    def unregister_ws(self, client):
        """Unregister a WebSocket client."""
        self._ws_clients.discard(client)

    async def broadcast_state(self):
        """Push current state to all WebSocket clients."""
        if not self._ws_clients:
            return
        state = await self.get_state()
        payload = json.dumps({"type": "brain_state", "data": state}, ensure_ascii=False)
        dead = set()
        for client in self._ws_clients:
            try:
                await client.send_text(payload)
            except Exception:
                dead.add(client)
        self._ws_clients -= dead
