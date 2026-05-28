"""v3.1 Session Router - Agent conversation auto-ingest endpoint.

POST /api/v1/session/append  - Agent calls after each conversation turn
POST /api/v1/session/batch    - Batch write multiple turns

Writes directly to ingest_feed.jsonl. The 30s scheduler picks it up automatically.
Zero user operation required.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("brain-memory.session")

FEED_PATH = Path(__file__).parent.parent / "ingest_feed.jsonl"

router = APIRouter(prefix="/api/v1/session", tags=["session"])


class SessionTurn(BaseModel):
    speaker: str = Field(..., description="speaker: user / agent / system")
    text: str = Field(..., min_length=1, description="conversation content")
    goal: str | None = Field(None, description="current session goal")
    metadata: dict | None = Field(None, description="extra metadata")


class SessionAppendResponse(BaseModel):
    accepted: bool
    written_at: str
    message: str


def _write_feed(text: str, source: str, explicit_mark: bool = False, metadata: dict | None = None):
    entry = {
        "text": text,
        "source": source,
        "explicit_mark": explicit_mark,
        "metadata": metadata or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(FEED_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logger.exception("session: write feed failed")
        return False


@router.post("/append", response_model=SessionAppendResponse)
async def session_append(turn: SessionTurn):
    formatted = f"[{turn.speaker}]: {turn.text}"

    emotional_triggers = [
        "bug", "error", "fail", "crash", "\u5d29\u6e83", "\u62a5\u9519", "\u5f02\u5e38",
        "fixed", "solved", "\u641e\u5b9a", "\u4fee\u590d", "\u89e3\u51b3", "\u7a81\u7834", "\u7ec8\u4e8e",
        "conflict", "\u51b2\u7a81", "contradict", "\u4e0d\u4e00\u81f4",
    ]
    has_emotional = any(kw in turn.text.lower() for kw in emotional_triggers)

    metadata = turn.metadata or {}
    if turn.goal:
        metadata["goal"] = turn.goal

    success = _write_feed(
        text=formatted,
        source=f"session:{turn.speaker}",
        explicit_mark=has_emotional,
        metadata=metadata,
    )

    if success:
        return SessionAppendResponse(
            accepted=True,
            written_at=datetime.now(timezone.utc).isoformat(),
            message=f"turn from '{turn.speaker}' written to feed"
        )
    else:
        return SessionAppendResponse(
            accepted=False,
            written_at="",
            message="write failed"
        )


@router.post("/batch")
async def session_batch(turns: list[SessionTurn]):
    written = 0
    for turn in turns:
        formatted = f"[{turn.speaker}]: {turn.text}"
        metadata = turn.metadata or {}
        if turn.goal:
            metadata["goal"] = turn.goal
        if _write_feed(text=formatted, source=f"session:{turn.speaker}", metadata=metadata):
            written += 1
    return {"total": len(turns), "accepted": written}
