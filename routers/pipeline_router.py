"""v3.0 脑区管线 API — 供 Web 控制台测试。"""
from fastapi import APIRouter
from pydantic import BaseModel
from services.pipeline import pipeline_ingest, pipeline_retrieve

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])


class PipelineIngestRequest(BaseModel):
    text: str
    source: str = "dashboard"
    bypass_gate: bool = True
    metadata: dict | None = None


class PipelineRetrieveRequest(BaseModel):
    query: str
    current_goal: str | None = None
    current_risk: str | None = None
    top_k: int = 5


@router.post("/ingest")
async def ingest(req: PipelineIngestRequest):
    """通过脑区管线摄入一条记忆（旁路模式，不经过门控）。"""
    return await pipeline_ingest(req.text, req.source, req.metadata, bypass_gate=req.bypass_gate)


@router.post("/retrieve")
async def retrieve(req: PipelineRetrieveRequest):
    """通过脑区管线检索记忆。"""
    return await pipeline_retrieve(req.query, req.current_goal, req.current_risk, req.top_k)
