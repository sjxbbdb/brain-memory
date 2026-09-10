"""Brain Memory v0.1 API — FastAPI + WebSocket.

REST:
  GET  /api/v4/state        — 当前脑状态
  POST /api/v4/input        — 外部输入
  GET  /api/v4/monologue    — 内在独白
  GET  /api/v4/health       — 健康检查
  GET  /api/v11/tasks       — 分层长期任务队列
  GET  /api/v11/autonomy    — 自主经历状态与有限历史
  GET  /api/v13/tasks       — 只读执行计划列表
  GET  /api/v13/tasks/{plan_id} — 只读执行计划详情
  GET  /api/v13/metrics     — 执行/学习指标摘要

WebSocket:
  ws://host:8001/ws         — 实时脑状态推送
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the brain-memory project root is on the path when launched as a module.
_sys_path_root = Path(__file__).parent.parent
if str(_sys_path_root) not in sys.path:
    sys.path.insert(0, str(_sys_path_root))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from brain.core import Brain
from brain.core_purpose import core_purpose  # V8
from agent.tool_registry import discover_tools, registry
from agent_bridge import AgentBridge
from config import (
    HOST,
    PORT,
    AGENT_BRIDGE_ENABLED,
    AGENT_BRIDGE_ALLOW_WRITE_TOOLS,
)
from version import PRODUCT_VERSION, PRODUCT_VERSION_LABEL

logger = logging.getLogger("brain-v5.api")

# ── Global brain instance ──
_brain: Brain | None = None
_bridge: AgentBridge | None = None
_API_RESERVED_SOURCES = {"creator", "self"}
_API_REQUESTOR = "api"


def get_brain() -> Brain:
    assert _brain is not None, "Brain not initialized"
    return _brain


def _sanitize_api_source(source: str) -> str:
    """Keep request-body labels from granting trusted in-process identity.

    ``creator`` and ``self`` are meaningful only to trusted in-process
    callers.  The HTTP body is untrusted—even on localhost—so a client cannot
    claim either label and bypass the self-boundary.  Session labels for
    ordinary callers are retained for isolation.
    """
    normalized = str(source or "external").strip()[:200] or "external"
    if (
        normalized in _API_RESERVED_SOURCES
        or normalized in {"internal", "none"}
        or normalized.startswith("agent/")
    ):
        return "external"
    return normalized


def _shareable_memories(brain: Brain, memories: list[dict]) -> list[dict]:
    """Apply the memory boundary to every HTTP memory response."""
    boundary = brain.brain_stem.boundary
    if boundary is None:
        return memories
    return [
        memory for memory in memories
        if isinstance(memory, dict)
        and boundary.should_share_memory(str(memory.get("id", "")), _API_REQUESTOR)
    ]


def _bounded_limit(value: int, default: int = 20, maximum: int = 100) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _compact_task_scheduler(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {
            "enabled": False,
            "running_goal_id": None,
            "queue_size": 0,
            "max_queue": 0,
            "tier_counts": {},
        }
    queue = snapshot.get("queue", [])
    return {
        "enabled": True,
        "version": snapshot.get("version", 1),
        "running_goal_id": snapshot.get("running_goal_id"),
        "last_selected_goal_id": snapshot.get("last_selected_goal_id"),
        "last_tick": snapshot.get("last_tick", 0),
        "queue_size": len(queue) if isinstance(queue, list) else 0,
        "max_queue": snapshot.get("max_queue", 0),
        "tier_counts": snapshot.get("tier_counts", {}),
        "total_preemptions": snapshot.get("total_preemptions", 0),
        "total_evicted": snapshot.get("total_evicted", 0),
        "total_expired": snapshot.get("total_expired", 0),
        "total_budget_exhausted": snapshot.get("total_budget_exhausted", 0),
        "total_user_submitted": snapshot.get("total_user_submitted", 0),
    }


def _read_only_task_scheduler_snapshot(scheduler: Any, goal_system: Any) -> dict[str, Any]:
    """Read scheduler state without running a mutating synchronization pass.

    ``LongTermTaskScheduler.sync`` normalizes legacy goals and may evict,
    expire, or otherwise rewrite queue state.  GET observability surfaces use
    the explicit read-only method when available and fall back to the
    historically pure ``snapshot`` method for older scheduler
    implementations.  A failing optional diagnostic must not turn the
    endpoint into a write path or make it unavailable.
    """
    if scheduler is None:
        return {}
    try:
        reader = getattr(scheduler, "read_only_snapshot", None)
        if callable(reader):
            value = reader(goal_system)
        else:
            value = scheduler.snapshot(goal_system)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _compact_task_execution_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    return {
        "id": plan.get("id", ""),
        "goal_id": plan.get("goal_id", ""),
        "objective": plan.get("objective", ""),
        "status": plan.get("status", ""),
        "active_step_id": plan.get("active_step_id", ""),
        "steps": plan.get("steps", 0),
        "completed_steps": plan.get("completed_steps", 0),
        "revision": plan.get("revision", 0),
    }


def _compact_task_execution_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        return {}
    return {
        "id": step.get("id", ""),
        "description": step.get("description", ""),
        "status": step.get("status", ""),
        "action_type": step.get("action_type", ""),
        "tool_name": step.get("tool_name", ""),
        "expected": step.get("expected", ""),
        "dependencies": step.get("dependencies", []),
        "attempts": step.get("attempts", 0),
        "retries": step.get("retries", 0),
        "replan_count": step.get("replan_count", 0),
        "created_tick": step.get("created_tick", 0),
        "started_tick": step.get("started_tick"),
        "completed_tick": step.get("completed_tick"),
        "last_action_id": step.get("last_action_id", ""),
        "result_quality": step.get("result_quality", ""),
        "result_summary": step.get("result_summary", ""),
        "blocked_reason": step.get("blocked_reason", ""),
    }


def _compact_task_execution_detail(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    steps = detail.get("steps", [])
    return {
        "id": detail.get("id", ""),
        "objective": detail.get("objective", ""),
        "goal_id": detail.get("goal_id", ""),
        "source": detail.get("source", ""),
        "status": detail.get("status", ""),
        "budget_ticks": detail.get("budget_ticks", 0),
        "deadline_tick": detail.get("deadline_tick", 0),
        "consumed_ticks": detail.get("consumed_ticks", 0),
        "created_tick": detail.get("created_tick", 0),
        "updated_tick": detail.get("updated_tick", 0),
        "created_at": detail.get("created_at", ""),
        "active_step_id": detail.get("active_step_id", ""),
        "risk_level": detail.get("risk_level", "low"),
        "constraints": detail.get("constraints", {}),
        "revision": detail.get("revision", 0),
        "steps": [
            _compact_task_execution_step(step)
            for step in steps
            if isinstance(step, dict)
        ],
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "action_count": len(detail.get("actions", [])) if isinstance(detail.get("actions", []), list) else 0,
        "outcome_count": len(detail.get("outcomes", [])) if isinstance(detail.get("outcomes", []), list) else 0,
        "replan_count": len(detail.get("replans", [])) if isinstance(detail.get("replans", []), list) else 0,
    }


def _compact_task_execution_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {
            "version": 1,
            "metrics": {},
            "plans": [],
        }
    plans = summary.get("plans", [])
    return {
        "version": summary.get("version", 1),
        "metrics": _compact_task_execution_metrics(summary.get("metrics", {})),
        "plans": [
            _compact_task_execution_plan(plan)
            for plan in plans
            if isinstance(plan, dict)
        ],
        "plan_count": len(plans) if isinstance(plans, list) else 0,
    }


def _compact_task_execution_metrics(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    return {
        "plans": metrics.get("plans", 0),
        "steps": metrics.get("steps", 0),
        "actions": metrics.get("actions", 0),
        "observations": metrics.get("observations", 0),
        "outcomes": metrics.get("outcomes", 0),
        "completed_plans": metrics.get("completed_plans", 0),
        "terminal_plans": metrics.get("terminal_plans", 0),
        "completion_rate": metrics.get("completion_rate", 0),
        "recovered_actions": metrics.get("recovered_actions", 0),
    }


def _compact_learning_summary(snapshot: Any) -> dict[str, Any]:
    # An empty/missing snapshot means that the optional learning sink is not
    # present.  Do not report it as enabled merely because the compactor was
    # handed a fallback ``{}`` after an exception.
    if not isinstance(snapshot, dict) or not snapshot:
        return {
            "enabled": False,
            "history_size": 0,
            "stats": {},
        }
    stats = snapshot.get("stats", {})
    history = snapshot.get("history", [])
    return {
        "enabled": True,
        "version": snapshot.get("version", 1),
        "history_size": len(history) if isinstance(history, list) else 0,
        "limits": snapshot.get("limits", {}),
        "stats": {
            "total_processed": stats.get("total_processed", 0),
            "total_positive": stats.get("total_positive", 0),
            "total_negative": stats.get("total_negative", 0),
            "total_quarantined": stats.get("total_quarantined", 0),
            "total_rejected": stats.get("total_rejected", 0),
            "total_duplicates": stats.get("total_duplicates", 0),
            "component_errors": stats.get("component_errors", 0),
        },
    }


def _safe_task_execution_metrics(execution: Any) -> dict[str, Any]:
    if execution is None:
        return {"enabled": False}
    try:
        return {
            "enabled": True,
            **_compact_task_execution_metrics(execution.metrics()),
        }
    except Exception:
        return {"enabled": False}


def _safe_learning_summary(learning: Any) -> dict[str, Any]:
    if learning is None:
        return _compact_learning_summary({})
    try:
        snapshot = learning.snapshot()
        return _compact_learning_summary(snapshot)
    except Exception:
        return _compact_learning_summary({})


def _safe_drive_metrics(drive_engine: Any) -> dict[str, Any]:
    """Expose bounded verified-feedback counters without mutating drives."""
    if drive_engine is None:
        return {
            "enabled": False,
            "total_verified_outcomes": 0,
            "total_verified_successes": 0,
            "total_verified_failures": 0,
            "last_verified_outcome_tick": 0,
        }
    try:
        snapshot = drive_engine.snapshot()
        if not isinstance(snapshot, dict):
            raise TypeError("drive snapshot is not a mapping")
        return {
            "enabled": True,
            "total_verified_outcomes": max(
                0, int(snapshot.get("total_verified_outcomes", 0))
            ),
            "total_verified_successes": max(
                0, int(snapshot.get("total_verified_successes", 0))
            ),
            "total_verified_failures": max(
                0, int(snapshot.get("total_verified_failures", 0))
            ),
            "last_verified_outcome_tick": max(
                0, int(snapshot.get("last_verified_outcome_tick", 0))
            ),
        }
    except Exception:
        # Optional observability must not make the health/metrics endpoint
        # fail, and must never call a mutating drive tick as a fallback.
        return {"enabled": False, "error": "unavailable"}


def _safe_task_execution_summary(execution: Any) -> dict[str, Any]:
    if execution is None:
        return _compact_task_execution_summary({})
    try:
        return _compact_task_execution_summary(execution.summary())
    except Exception:
        return _compact_task_execution_summary({})


def _safe_task_execution_detail(execution: Any, plan_id: str) -> dict[str, Any]:
    if execution is None:
        return {}
    try:
        return _compact_task_execution_detail(execution.detail(plan_id=plan_id))
    except Exception:
        return {}


# ── Lifecycle ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _brain, _bridge
    _brain = Brain()
    _bridge = None
    broadcaster = None
    bridge = None
    try:
        # Registration is safe at startup; actual execution remains behind the
        # explicit AgentBridge boundary.  A broken optional module should not
        # prevent the core heartbeat from coming up.
        try:
            discovered_tools = discover_tools()
        except Exception as exc:
            discovered_tools = []
            logger.warning("tool discovery failed: %s", str(exc)[:120])

        await _brain.wake_up()
        if AGENT_BRIDGE_ENABLED:
            try:
                # Keep the execution boundary in the same event loop as the
                # heartbeat.  The bridge defaults to read-only tools; any
                # write-capable tool requires an explicit operator flag.
                bridge = AgentBridge(
                    _brain.brain_stem,
                    registry,
                    workspace_root=_sys_path_root,
                    allow_write_tools=AGENT_BRIDGE_ALLOW_WRITE_TOOLS,
                )
                await bridge.start()
                _bridge = bridge
            except Exception as exc:
                bridge = None
                _bridge = None
                logger.warning("agent bridge failed to start: %s", str(exc)[:120])
        # Start broadcasting only after the heartbeat is live.  Otherwise a
        # startup failure leaves an orphan background task behind.
        broadcaster = asyncio.create_task(_broadcast_loop())
        logger.info(
            "Brain %s API + bounded autonomy started (tools=%s, bridge=%s)",
            PRODUCT_VERSION_LABEL,
            discovered_tools,
            bool(bridge),
        )
        yield
    finally:
        if broadcaster is not None:
            broadcaster.cancel()
            try:
                await broadcaster
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("state broadcaster stopped with an error: %s", str(exc)[:120])
        if bridge is not None:
            try:
                await bridge.stop()
            except Exception as exc:
                logger.warning("agent bridge shutdown encountered an error: %s", str(exc)[:120])
        _bridge = None
        if _brain is not None:
            try:
                await _brain.sleep()
            except Exception as exc:
                # Shutdown should remain best-effort even when a storage
                # handle or optional subsystem failed during startup.
                logger.warning("brain shutdown encountered an error: %s", str(exc)[:120])
        logger.info("Brain %s API + bounded autonomy stopped", PRODUCT_VERSION_LABEL)


async def _broadcast_loop():
    """Push brain state to WebSocket clients every 3 seconds."""
    while True:
        await asyncio.sleep(3)
        brain = _brain
        if brain and brain.is_awake:
            await brain.broadcast_state()


# ── App ──

app = FastAPI(
    title=f"Brain Memory {PRODUCT_VERSION_LABEL}",
    version=PRODUCT_VERSION,
    lifespan=lifespan,
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv("BRAIN_MEMORY_CORS_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    # Same-origin dashboard access does not need CORS.  Operators who expose
    # a separate frontend must opt in with a comma-separated allow-list.
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST Endpoints ──

class InputRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000, description="input text from calling agent")
    source: str = Field("external", min_length=1, max_length=200, description="source identifier")
    goal: str | None = Field(
        None,
        max_length=500,
        description="explicit user task to add to the bounded long-term queue",
    )
    episode_id: str | None = Field(None, max_length=80, description="optional autonomous episode correlation ID")
    intent_id: str | None = Field(None, max_length=80, description="optional intent correlation ID")
    goal_id: str | None = Field(None, max_length=100, description="optional goal correlation ID")
    plan_id: str | None = Field(None, max_length=100, description="optional V13 execution plan correlation ID")
    step_id: str | None = Field(None, max_length=100, description="optional V13 execution step correlation ID")
    action_id: str | None = Field(None, max_length=100, description="optional V13 execution action correlation ID")


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
        source=_sanitize_api_source(req.source),
        goal=req.goal,
        episode_id=req.episode_id,
        intent_id=req.intent_id,
        goal_id=req.goal_id,
        plan_id=req.plan_id,
        step_id=req.step_id,
        action_id=req.action_id,
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
    memories = _shareable_memories(brain, await brain.get_identity_memories(_bounded_limit(limit)))
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
    ms = brain.memory_store
    recent = _shareable_memories(brain, ms.search("", limit=_bounded_limit(limit)))
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
    results = _shareable_memories(brain, ms.search(q[:4000], limit=_bounded_limit(limit)))
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
    loop_task = brain.brain_stem._task
    loop_running = bool(loop_task and not loop_task.done())
    task_scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    # Health is a GET/observability path.  Synchronization belongs to the
    # heartbeat/tick owner; invoking it here could expire goals, evict queue
    # entries, or rewrite goal status as a side effect of an HTTP read.
    task_snapshot = _read_only_task_scheduler_snapshot(
        task_scheduler, getattr(brain.brain_stem, "goal_system", None)
    )
    return {
        "status": (
            "awake" if brain.is_awake and loop_running
            else "degraded" if brain.is_awake
            else "asleep"
        ),
        "version": PRODUCT_VERSION,
        "product_version": PRODUCT_VERSION,
        "version_label": PRODUCT_VERSION_LABEL,
        "loop_running": loop_running,
        "total_ticks": state.get("total_ticks", 0),
        "uptime_seconds": state.get("uptime_seconds", 0.0),
        "emotion": state.get("current_emotion", "neutral"),
        "focus": state.get("focus_entity"),
        "memory_count": brain.memory_store.count(),
        "identity_memory_count": len(
            _shareable_memories(brain, brain.memory_store.get_identity_memories(100))
        ),
        "llm_error_count": state.get("llm_error_count", 0),
        "last_error": state.get("last_error", ""),
        "loop_error_count": state.get("loop_error_count", 0),
        "last_loop_error": state.get("last_loop_error", ""),
        "last_heartbeat_at": state.get("last_heartbeat_at", ""),
        "active_sessions": brain.brain_stem.state.session_manager.get_session_count(),
        "sleep_state": brain.brain_stem.sleep_state,
        # Constitutional lifecycle projection; the immutable ledger remains
        # local and is never exposed through this compact health response.
        "life": state.get("life", {}),
        "autonomy": state.get("autonomy", {}),
        "tasks": {
            "running_goal_id": task_snapshot.get("running_goal_id"),
            "queue_size": len(task_snapshot.get("queue", [])),
            "max_queue": task_snapshot.get("max_queue", 0),
            "tier_counts": task_snapshot.get("tier_counts", {}),
        },
        "execution": _safe_task_execution_metrics(
            getattr(brain.brain_stem, "task_execution", None)
        ),
        "learning": _safe_learning_summary(
            getattr(brain.brain_stem, "learning_feedback", None)
        ),
        "drive_feedback": _safe_drive_metrics(
            getattr(brain.brain_stem, "drive_engine", None)
        ),
        "bridge": _bridge.snapshot() if _bridge is not None else {
            "enabled": bool(AGENT_BRIDGE_ENABLED),
            "running": False,
        },
    }


@app.get("/api/v4/goals")
async def get_goals():
    """v5.1: Get the brain's active goals and goal statistics."""
    brain = get_brain()
    gs = brain.brain_stem.goal_system
    snapshot = gs.snapshot()
    scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    if scheduler is not None:
        snapshot["task_scheduler"] = _read_only_task_scheduler_snapshot(scheduler, gs)
    return snapshot


@app.get("/api/v11/tasks")
async def get_long_term_tasks():
    """V11: inspect the bounded, tiered long-term task queue."""
    brain = get_brain()
    scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    if scheduler is None:
        return {"enabled": False, "queue": []}
    return _read_only_task_scheduler_snapshot(scheduler, brain.brain_stem.goal_system)


@app.get("/api/v11/autonomy")
async def get_autonomy():
    """V11: inspect the bounded autonomous episode ledger."""
    brain = get_brain()
    manager = getattr(brain.brain_stem, "autonomy", None)
    if manager is None:
        return {
            "enabled": False,
            "status": "disabled",
            "active": None,
            "history": [],
        }
    snapshot = manager.snapshot()
    snapshot["enabled"] = True
    return snapshot


@app.get("/api/v13/tasks")
async def get_v13_tasks():
    """V13: inspect the read-only execution plan ledger."""
    brain = get_brain()
    execution = getattr(brain.brain_stem, "task_execution", None)
    scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    scheduler_snapshot = _read_only_task_scheduler_snapshot(
        scheduler, brain.brain_stem.goal_system
    )
    if execution is None:
        return {
            "enabled": False,
            "execution": _compact_task_execution_summary({}),
            "scheduler": _compact_task_scheduler(scheduler_snapshot),
        }
    return {
        "enabled": True,
        "execution": _safe_task_execution_summary(execution),
        "scheduler": _compact_task_scheduler(scheduler_snapshot),
    }


@app.get("/api/v13/tasks/{plan_id}")
async def get_v13_task(plan_id: str):
    """V13: inspect one read-only execution plan by plan_id."""
    brain = get_brain()
    execution = getattr(brain.brain_stem, "task_execution", None)
    if execution is None:
        raise HTTPException(status_code=404, detail="task execution is disabled")
    detail = _safe_task_execution_detail(execution, plan_id)
    if not detail:
        raise HTTPException(status_code=404, detail="plan not found")
    scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    scheduler_snapshot = _read_only_task_scheduler_snapshot(
        scheduler, brain.brain_stem.goal_system
    )
    return {
        "enabled": True,
        "plan": detail,
        "scheduler": _compact_task_scheduler(scheduler_snapshot),
    }


@app.get("/api/v13/metrics")
async def get_v13_metrics():
    """V13: inspect execution and learning metrics without raw tool payloads."""
    brain = get_brain()
    execution = getattr(brain.brain_stem, "task_execution", None)
    learning = getattr(brain.brain_stem, "learning_feedback", None)
    scheduler = getattr(brain.brain_stem, "task_scheduler", None)
    scheduler_snapshot = _read_only_task_scheduler_snapshot(
        scheduler, brain.brain_stem.goal_system
    )
    execution_summary = _safe_task_execution_summary(execution)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(execution is not None or learning is not None),
        "execution": {
            "enabled": execution is not None,
            "metrics": _safe_task_execution_metrics(execution),
            "summary": execution_summary,
            "plan_count": execution_summary.get("plan_count", 0),
        },
        "learning": _safe_learning_summary(learning),
        "drive_feedback": _safe_drive_metrics(
            getattr(brain.brain_stem, "drive_engine", None)
        ),
        "scheduler": _compact_task_scheduler(scheduler_snapshot),
    }


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


# ── V6 State Field ──

@app.get("/api/v6/state-field")
async def get_state_field():
    """V6: Get the ActivationField state — all 14 dimensions + diffusion rules."""
    brain = get_brain()
    activation = brain.brain_stem.state.activation
    return {
        "values": activation.to_dict(),
        "dominant_dimensions": [
            {"name": name, "value": round(val, 3),
             "label_cn": activation.dimension_labels.get(name, name)}
            for name, val in activation.dominant_dimensions(5)
        ],
        "narrative": activation.narrative(),
        "diffusion": activation.diffusion.snapshot(),
        "diffusion_matrix": activation.diffusion.get_weights_matrix(),
        "closures": activation.detect_closure(),
        "history_length": len(activation.history),
        "total_ticks": activation.total_ticks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/v6/working-memory-state")
async def get_working_memory_state():
    """V6: Get SalienceScore-based working memory state."""
    brain = get_brain()
    wm = brain.brain_stem.working_memory
    activation = brain.brain_stem.state.activation
    return {
        "items": wm.get_state_snapshot(),
        "capacity": 7,
        "current_context": wm.get_context(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── V7 Drive Engine ──

@app.get("/api/v7/drives")
async def get_drives():
    """V7: Get all 7 drive intensities from ActivationField."""
    brain = get_brain()
    activation = brain.brain_stem.state.activation
    de = brain.brain_stem.drive_engine
    drives = {}
    for name in ["survival_drive","curiosity_drive","coherence_drive",
                 "growth_drive","exploration_drive","creation_drive","connection_drive"]:
        drives[name] = round(activation.get(name), 3)
    return {
        "drives": drives,
        "dominant": max(drives, key=drives.get),
        "signals_emitted": de.total_signals_emitted,
        "goals_generated": de.total_goals_generated,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── V8 Exploration + Reflection ──

@app.get("/api/v8/exploration")
async def get_exploration():
    """V8: Exploration queue status."""
    brain = get_brain()
    eq = brain.brain_stem.exploration_queue
    ex = brain.brain_stem.exploration_executor
    return {
        "queue": eq.snapshot(),
        "executor": ex.snapshot(),
        "core_purpose": str(core_purpose),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/v8/traits")
async def get_traits():
    """V8: Get behavioral traits and their modulation effects."""
    brain = get_brain()
    sm = brain.brain_stem.state.self_model
    return {
        "traits": dict(sm.behavioral_traits),
        "modulation_examples": {
            "call_tool": round(sm.modulate_intent("call_tool", 0.7), 3),
            "respond": round(sm.modulate_intent("respond", 0.7), 3),
            "think": round(sm.modulate_intent("think", 0.7), 3),
        },
        "identity_version": sm.identity_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/v8/reflection")
async def get_reflection():
    """V8: Reflection engine stats."""
    brain = get_brain()
    return brain.brain_stem.reflection_engine.snapshot()


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
