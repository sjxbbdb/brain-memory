"""认知检索路由。"""
from fastapi import APIRouter, Query
from models.schemas import RetrievalQuery, RetrievalResult
from services import retrieval as ret_svc
from services import memory_service as svc

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


@router.post("/search", response_model=list[RetrievalResult])
async def search(query: RetrievalQuery):
    return await ret_svc.cognitive_search(query)


@router.get("/recent")
async def recent_memories(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
):
    mems = await svc.list_memories(sort_by="created", order="desc", limit=limit)
    return [m for m in mems]
