"""认知检索路由。"""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from models.schemas import RetrievalQuery, RetrievalResult
from services import retrieval as ret_svc
from services import memory_service as svc

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


@router.post("/search", response_model=list[RetrievalResult])
async def search(query: RetrievalQuery):
    result = await ret_svc.cognitive_search(query)
    return result["results"]


@router.get("/recent")
async def recent_memories(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
):
    mems = await svc.list_memories(sort_by="created", order="desc", limit=limit)
    return [m for m in mems]


class FeedbackRequest(BaseModel):
    log_id: int
    helpful: bool

@router.post("/retrieval/feedback")
async def record_feedback(req: FeedbackRequest):
    """Record retrieval feedback: was this search result helpful?"""
    from models.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "UPDATE retrieval_log SET feedback = ? WHERE id = ?",
            (1 if req.helpful else -1, req.log_id)
        )
        await db.commit()
        return {"status": "ok", "log_id": req.log_id, "helpful": req.helpful}
    finally:
        await db.close()
