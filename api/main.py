"""Brain Memory v5.0 API — FastAPI + WebSocket.

REST:
  GET  /api/v4/state        — 当前脑状态
  POST /api/v4/input        — 外部输入
  GET  /api/v4/monologue    — 内在独白
  GET  /api/v4/health       — 健康检查

WebSocket:
  ws://host:8001/ws         — 实时脑状态推送
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# Ensure brain-memory-v5.4 is on the path
_sys_path_root = Path(__file__).parent.parent
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from brain.core import Brain
from config import HOST, PORT

logger = logging.getLogger("brain-v5.api")

# ── Global brain instance ──
_brain: Brain | None = None


def get_brain() -> Brain:
    assert _brain is not None, "Brain not initialized"
    return _brain


# ── Lifecycle ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _brain
    _brain = Brain()

    # Start background state broadcaster
    broadcaster = asyncio.create_task(_broadcast_loop())

    await _brain.wake_up()
    logger.info("Brain v5.0 API started")

    yield

    await _brain.sleep()
    broadcaster.cancel()
    logger.info("Brain v5.0 API stopped")


async def _broadcast_loop():
    """Push brain state to WebSocket clients every 3 seconds."""
    while True:
        await asyncio.sleep(3)
        brain = _brain
        if brain and brain.is_awake:
            await brain.broadcast_state()


# ── App ──

app = FastAPI(title="Brain Memory v5.4", version="5.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST Endpoints ──

class InputRequest(BaseModel):
    text: str = Field(..., min_length=1, description="input text from calling agent")
    source: str = Field("external", description="source identifier")
    goal: str | None = Field(None, description="current goal")


@app.get("/api/v4/state")
async def get_state():
    """Get current brain state."""
    brain = get_brain()
    state = await brain.get_state()
    state["sleep_state"] = brain.brain_stem.sleep_state
    return state


@app.post("/api/v4/input")
async def post_input(req: InputRequest):
    """Send input to the brain. Returns context for the calling agent."""
    brain = get_brain()
    result = await brain.process_input(
        text=req.text,
        source=req.source,
        goal=req.goal,
    )
    # 立即推送最新状态给所有 WebSocket 客户端
    await brain.broadcast_state()
    return result


@app.get("/api/v4/monologue")
async def get_monologue():
    """Get the brain's current inner monologue."""
    brain = get_brain()
    return {
        "monologue": await brain.get_inner_monologue(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }




@app.get("/api/v4/identity-memories")
async def get_identity_memories(limit: int = 20):
    """Get memories that shaped the brain's identity."""
    brain = get_brain()
    memories = await brain.get_identity_memories(limit)
    return {
        "count": len(memories),
        "memories": [
            {
                "id": m.get("id"),
                "title": m.get("title"),
                "summary": m.get("summary", "")[:100],
                "importance": m.get("importance"),
                "emotion_label": m.get("emotion_label"),
                "created": m.get("created"),
            }
            for m in memories
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }



@app.get("/api/v4/memory-timeline")
async def get_memory_timeline(limit: int = 20):
    """Get recent memories as a timeline."""
    brain = get_brain()
    from storage.database import MemoryStore
    ms = brain.memory_store
    recent = ms.search("", limit=limit)
    return {
        "count": len(recent),
        "memories": [
            {
                "id": m.get("id"),
                "title": m.get("title", ""),
                "summary": m.get("summary", "")[:80],
                "type": m.get("type"),
                "importance": m.get("importance"),
                "emotion_label": m.get("emotion_label"),
                "is_identity_forming": m.get("is_identity_forming"),
                "created": m.get("created"),
            }
            for m in recent
        ],
    }

@app.get("/api/v4/memory/search")
async def search_memories(q: str = "", limit: int = 20):
    """Search memories by keyword query."""
    brain = get_brain()
    ms = brain.memory_store
    results = ms.search(q, limit=limit)
    return {
        "query": q,
        "count": len(results),
        "memories": [
            {
                "id": m.get("id"),
                "title": m.get("title", ""),
                "summary": m.get("summary", "")[:120],
                "type": m.get("type"),
                "importance": m.get("importance"),
                "emotion_label": m.get("emotion_label"),
                "is_identity_forming": m.get("is_identity_forming"),
                "entities": m.get("entities"),
                "source": m.get("source"),
                "created": m.get("created"),
            }
            for m in results
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v4/sessions")
async def get_sessions():
    """Get all active sessions and their states."""
    brain = get_brain()
    return brain.brain_stem.state.session_manager.all_snapshots()

@app.get("/api/v4/self")
async def get_self():
    """Get the brain's self-model — who it thinks it is."""
    brain = get_brain()
    state = await brain.get_state()
    sm = state.get("self_model", {})
    curiosity_data = state.get("curiosity", {})
    return {
        "identity": sm.get("identity_anchor", ""),
        "traits": sm.get("identity_traits", []),
        "version": sm.get("identity_version", 1),
        "drives": sm.get("drives", {}),
        "mood_tendency": sm.get("mood_tendency", "balanced"),
        "total_experiences": sm.get("total_experiences", 0),
        "emotional_baseline": sm.get("emotional_baseline", {}),
        "attention_biases": sm.get("attention_biases", {}),
        "last_reflection": sm.get("last_reflection", ""),
        "recent_shifts": sm.get("identity_shifts", [])[-5:],
        "curiosity": {
            "open_questions": [
                {"question": q.get("question"), "drive": q.get("drive_label")}
                for q in curiosity_data.get("open_questions", [])[-10:]
            ],
            "pending_count": len(curiosity_data.get("open_questions", [])),
            "total_generated": curiosity_data.get("total_generated", 0),
            "total_resolved": curiosity_data.get("total_resolved", 0),
            "exploration_topics": curiosity_data.get("exploration_topics", [])[-5:],
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/v4/health")
async def health():
    brain = get_brain()
    state = await brain.get_state()
    return {
        "status": "awake" if brain.is_awake else "asleep",
        "version": "5.4.0",
        "total_ticks": state.get("total_ticks", 0),
        "uptime_seconds": state.get("uptime_seconds", 0.0),
        "emotion": state.get("current_emotion", "neutral"),
        "focus": state.get("focus_entity"),
        "memory_count": brain.memory_store.count(),
        "identity_memory_count": len(brain.memory_store.get_identity_memories(100)),
        "llm_error_count": state.get("llm_error_count", 0),
        "last_error": state.get("last_error", ""),
        "active_sessions": brain.brain_stem.state.session_manager.get_session_count(),
        "sleep_state": brain.brain_stem.sleep_state,
    }


@app.get("/api/v4/goals")
async def get_goals():
    """v5.1: Get the brain's active goals and goal statistics."""
    brain = get_brain()
    gs = brain.brain_stem.goal_system
    return gs.snapshot()


@app.get("/api/v4/metacognition")
async def get_metacognition():
    """v5.2: Get the brain's metacognitive state — self-awareness metrics."""
    brain = get_brain()
    mc = brain.brain_stem.metacognition
    return mc.snapshot()


@app.get("/api/v4/emotion")
async def get_emotion():
    """v5.3: Get the brain's emotional spectrum — continuous blends, trajectory, expression."""
    brain = get_brain()
    es = brain.brain_stem.emotional_spectrum
    return es.snapshot()


@app.get("/api/v4/skills")
async def get_skills():
    """v5.4: Get learned procedural skills."""
    brain = get_brain()
    return brain.brain_stem.procedural_memory.snapshot()


@app.get("/api/v4/timesense")
async def get_timesense():
    """v5.4: Get time awareness — rhythm, temporal narrative, age."""
    brain = get_brain()
    return brain.brain_stem.time_sense.snapshot()


# ── WebSocket ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    brain = get_brain()
    brain.register_ws(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type", "")

            if msg_type == "input":
                result = await brain.process_input(
                    text=msg.get("text", ""),
                    source="websocket",
                    goal=msg.get("goal"),
                )
                await ws.send_text(json.dumps({
                    "type": "input_response",
                    "data": result,
                }, ensure_ascii=False))

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        brain.unregister_ws(ws)


# ── Entry point ──



# ── Static files ──
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "static")
if _os.path.exists(_static_dir):
    from fastapi.staticfiles import StaticFiles
    # Mount static files at root (AFTER API routes, so API takes precedence)
    try:
        app.mount("/dashboard", StaticFiles(directory=_static_dir, html=True), name="dashboard")
    except Exception:
        pass

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    uvicorn.run("api.main:app", host=HOST, port=PORT, reload=False)
