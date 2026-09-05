"""Brain — v5.0 core agent. The brain memory system as a conscious agent.

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

from brain.brain_stem import BrainStem
from config import INPUT_TIMEOUT_SEC
from storage.database import init_db, StateStore, MemoryStore

logger = logging.getLogger("brain-v5.core")


class Brain:
    """Brain Memory v5.0 — a conscious memory agent."""

    def __init__(self, db_path: str = "brain_v4.db"):
        self.state_store = StateStore(db_path)
        self.memory_store = MemoryStore(db_path)
        self.brain_stem = BrainStem(
            state_store=self.state_store,
            memory_store=self.memory_store,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = None
        self._wake = False
        self._ws_clients: set = set()  # WebSocket clients

    # ── Lifecycle ──

    def _ensure_loop_primitives(self):
        """Keep lifecycle operations on one event loop at a time.

        ``BrainStem`` already enforces loop ownership for the heartbeat.  The
        wrapper lock needs the same guard; otherwise a second ``asyncio.run``
        can wait on a lock bound to the previous loop and fail with a delayed
        ``RuntimeError``.
        """
        current = asyncio.get_running_loop()
        if self._bound_loop is current:
            return
        stem_task = getattr(self.brain_stem, "_task", None)
        if self._bound_loop is not None and stem_task and not stem_task.done():
            raise RuntimeError(
                "brain cannot change event loops while awake; "
                "sleep it on the owning loop first"
            )
        lifecycle_lock = self._lifecycle_lock
        if self._bound_loop is not None and lifecycle_lock.locked():
            raise RuntimeError(
                "brain lifecycle transition is still running on another event loop"
            )
        active_waiters = [
            waiter for waiter in (getattr(lifecycle_lock, "_waiters", ()) or ())
            if not waiter.done()
        ]
        if active_waiters:
            raise RuntimeError(
                "brain lifecycle transition is still running on another event loop"
            )
        if self._bound_loop is not None:
            self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = current

    async def wake_up(self):
        """Wake the brain. Starts consciousness loop."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            # Initialize the exact database configured for this instance.  The
            # old call silently initialized the process-wide default path, which
            # broke isolated instances and restart tests.
            init_db(self.state_store.db_path)
            await self.brain_stem.start()
            self._wake = True
            logger.info("brain: awake")

    async def sleep(self):
        """Put the brain to sleep. Stops consciousness loop."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self.brain_stem.stop()
            # Release SQLite handles explicitly.  This matters on Windows, where
            # an open connection can prevent rotation/deletion of the local state
            # file and can make a restart look like data loss.
            self.state_store.close()
            self.memory_store.close()
            self._wake = False
            logger.info("brain: asleep")

    async def get_identity_memories(self, limit: int = 20) -> list[dict]:
        """Get memories that shaped the identity."""
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
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
        Uses a per-request completion future so concurrent callers remain
        isolated from one another.
        """
        # Validate loop ownership before inspecting or touching the stem.  A
        # caller that reuses a Brain from a different ``asyncio.run`` must get
        # an explicit lifecycle error, rather than a misleading "not awake"
        # response (or a cross-loop queue failure later).
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
        source = str(source or "external")[:200]
        loop_task = self.brain_stem._task
        if not self._wake or not loop_task or loop_task.done():
            return self.brain_stem._build_input_result(
                source=source,
                pending=False,
                error="brain not awake",
            )

        # ``internal`` is reserved for the brain's own spontaneous-thought
        # path.  An API caller must not be able to label arbitrary text as
        # internal and bypass external-input policy/accounting.
        if source in {"internal", "none"}:
            source = "external"

        # Submit through a per-request future.  A shared Event/last_intent
        # pair allowed concurrent callers to receive each other's result.
        request_id, done_future = self.brain_stem.submit_input(
            text=text,
            source=source,
            goal=goal,
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(done_future),
                timeout=INPUT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            # Keep the future alive so the actor can finish and clean it up,
            # but return an explicit pending result instead of stale state.
            return self.brain_stem._build_input_result(
                source=source,
                pending=True,
                request_id=request_id,
            )

    # ── Output ──

    async def next_output(self, timeout: float = 1.0) -> dict | None:
        """Get next output event from the brain's output feed."""
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
        try:
            return await asyncio.wait_for(self.brain_stem._output_feed.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ── State ──

    async def get_state(self) -> dict:
        """Get full brain state for external inspection."""
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
        return self.brain_stem.state.snapshot()

    async def get_inner_monologue(self) -> str:
        """Get the brain's current inner monologue."""
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
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
        self._ensure_loop_primitives()
        self.brain_stem._ensure_loop_primitives()
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
