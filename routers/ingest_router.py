"""Auto-Ingest 路由 — 通用摄入端点。

POST /api/v1/ingest
  任何智能体 POST 文本即可，自动推断参数 → 去重检测 → 门控 → 入库。
  
  请求体:
    {
      "text": "对话或事件文本",
      "source": "hermes",
      "explicit_mark": false,
      "force": false,          // 跳过去重检测
      "metadata": {}
    }
  
  响应:
    {
      "accepted": true/false,
      "gate_reason": "explicit_mark" | "emotional_signal" | ... | "discarded",
      "skip_reason": "duplicate" | null,
      "memory_id": "episodic-2026-05-27-001" | null,
      "inferred_type": "episodic",
      "importance": 0.55,
      ...
    }
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field
from services.auto_ingest import auto_ingest, detect_emotion, check_duplicate
from services.attention_gating import check_gate
from services import memory_service as svc

router = APIRouter(prefix="/api/v1", tags=["ingest"])


class IngestRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = Field("unknown")
    explicit_mark: bool = Field(False)
    force: bool = Field(False, description="跳过去重检测，强制摄入")
    metadata: dict | None = Field(None)


class IngestResponse(BaseModel):
    accepted: bool
    gate_reason: str
    skip_reason: str | None = None
    memory_id: str | None = None
    inferred_type: str
    emotion_label: str
    importance: float
    surprise_score: float
    title: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None


@router.post("/ingest", response_model=IngestResponse)
async def ingest_text(req: IngestRequest):
    # Step 1: 自动推断
    memory = auto_ingest(
        text=req.text, source=req.source,
        explicit_mark=req.explicit_mark, metadata=req.metadata,
    )
    
    # Step 2: 去重检测（force 模式跳过）
    if not req.force:
        dup = await check_duplicate(req.text, memory.title)
        if dup["is_duplicate"]:
            return IngestResponse(
                accepted=False,
                gate_reason="discarded",
                skip_reason="duplicate",
                inferred_type=memory.type,
                emotion_label=detect_emotion(req.text)[1],
                importance=memory.importance,
                surprise_score=memory.surprise_score,
                title=memory.title,
                is_duplicate=True,
                duplicate_of=dup["matched_id"],
            )
    
    # Step 3: 门控判定
    gate = check_gate(memory)
    
    if gate.passed:
        if "importance" in gate.adjusted_fields:
            memory.importance = gate.adjusted_fields["importance"]
        if "emotion_weight" in gate.adjusted_fields:
            memory.emotion_weight = gate.adjusted_fields["emotion_weight"]
        
        result = await svc.create_memory(memory)
        return IngestResponse(
            accepted=True,
            gate_reason=gate.reason,
            memory_id=result.id,
            inferred_type=memory.type,
            emotion_label=detect_emotion(req.text)[1],
            importance=memory.importance,
            surprise_score=memory.surprise_score,
            title=memory.title,
        )
    else:
        return IngestResponse(
            accepted=False,
            gate_reason=gate.reason,
            skip_reason="gate_rejected",
            inferred_type=memory.type,
            emotion_label=detect_emotion(req.text)[1],
            importance=memory.importance,
            surprise_score=memory.surprise_score,
            title=memory.title,
        )


@router.post("/ingest/batch")
async def ingest_batch(items: list[IngestRequest]):
    results = []
    for item in items:
        memory = auto_ingest(
            text=item.text, source=item.source,
            explicit_mark=item.explicit_mark, metadata=item.metadata,
        )
        
        # 去重
        if not item.force:
            dup = await check_duplicate(item.text, memory.title)
            if dup["is_duplicate"]:
                results.append({
                    "accepted": False, "skip_reason": "duplicate",
                    "duplicate_of": dup["matched_id"],
                    "title": memory.title,
                })
                continue
        
        gate = check_gate(memory)
        if gate.passed:
            if "importance" in gate.adjusted_fields:
                memory.importance = gate.adjusted_fields["importance"]
            if "emotion_weight" in gate.adjusted_fields:
                memory.emotion_weight = gate.adjusted_fields["emotion_weight"]
            result = await svc.create_memory(memory)
            results.append({
                "accepted": True, "gate_reason": gate.reason,
                "memory_id": result.id, "title": memory.title,
            })
        else:
            results.append({
                "accepted": False, "skip_reason": "gate_rejected",
                "title": memory.title,
            })
    return {"total": len(items), "accepted": sum(1 for r in results if r["accepted"]), "results": results}
