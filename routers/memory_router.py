"""记忆 CRUD 路由 — v3.0。"""
from fastapi import APIRouter, HTTPException, Query
from models.schemas import MemoryCreate, MemoryUpdate, MemoryResponse, MemoryVersion
from services.attention_gating import check_gate
from services import memory_service as svc

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.post("", response_model=MemoryResponse, status_code=201)
async def create_memory(data: MemoryCreate):
    gate = check_gate(data)
    if not gate.passed:
        raise HTTPException(status_code=422, detail=f"注意力门控拦截: {gate.reason}")

    if "importance" in gate.adjusted_fields:
        data.importance = gate.adjusted_fields["importance"]

    return await svc.create_memory(data)


@router.get("", response_model=list[MemoryResponse])
async def list_memories(
    layer: str | None = Query(None),
    type_: str | None = Query(None, alias="type"),
    sort_by: str = Query("created"),
    order: str = Query("desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
):
    return await svc.list_memories(layer, type_, sort_by, order, limit, offset, include_archived)


@router.get("/{memory_id}", response_model=MemoryResponse)
async def get_memory(memory_id: str):
    mem = await svc.get_memory(memory_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return mem


@router.get("/{memory_id}/versions")
async def get_memory_versions(memory_id: str):
    """v3.0: 获取记忆的覆写版本链（冷归档）。"""
    mem = await svc.get_memory(memory_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    versions = await svc.get_memory_versions(memory_id)
    return {"memory_id": memory_id, "current_version": mem.version, "versions": versions}


@router.put("/{memory_id}", response_model=MemoryResponse)
async def update_memory(memory_id: str, data: MemoryUpdate):
    mem = await svc.update_memory(memory_id, data)
    if mem is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return mem


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, hard: bool = Query(False)):
    if hard:
        ok = await svc.delete_memory(memory_id)
    else:
        ok = await svc.archive_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在或不可删除")
    return {"ok": True}
