"""睡眠巩固路由。"""
from fastapi import APIRouter, Query
from models.schemas import ConsolidationRequest, ConsolidationReport
from services import consolidation as cons_svc
from models.database import get_db

router = APIRouter(prefix="/api/consolidation", tags=["consolidation"])


@router.post("/run", response_model=ConsolidationReport)
async def run_consolidation(req: ConsolidationRequest | None = None):
    phases = req.phases if req and req.phases else None
    return await cons_svc.run_consolidation(phases)


@router.get("/logs")
async def consolidation_logs(limit: int = Query(20, ge=1, le=100)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM consolidation_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
