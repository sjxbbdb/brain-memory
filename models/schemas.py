"""Pydantic 请求/响应模型。"""
import json
from datetime import datetime
from pydantic import BaseModel, Field


# ── 请求模型 ──

class MemoryCreate(BaseModel):
    title: str
    content: str = ""
    type: str = "episodic"
    importance: float = 0.5
    novelty: float = 0.5
    failure_cost: float = 0.5
    goal_relevance: float = 0.5
    surprise_score: float = 0.3
    confidence: float = 0.8
    source: str = "direct_experience"
    tags: list[str] = []
    entities: list[str] = []
    relations: list[dict] = []
    explicit_mark: bool = False


class MemoryUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    type: str | None = None
    importance: float | None = None
    novelty: float | None = None
    failure_cost: float | None = None
    goal_relevance: float | None = None
    surprise_score: float | None = None
    confidence: float | None = None
    source: str | None = None
    tags: list[str] | None = None
    entities: list[str] | None = None
    relations: list[dict] | None = None


class RetrievalQuery(BaseModel):
    query: str
    current_goal: str | None = None
    current_risk: str | None = None
    top_k: int = 10


class ConsolidationRequest(BaseModel):
    phases: list[int] | None = None


# ── 响应模型 ──

class MemoryResponse(BaseModel):
    id: str
    type: str
    layer: str
    title: str
    content: str
    importance: float
    novelty: float
    failure_cost: float
    goal_relevance: float
    emotion_weight: float
    surprise_score: float
    confidence: float
    source: str
    corroboration_count: int
    access_count: int
    last_accessed: str | None
    decay_rate: float
    half_life_days: float
    created: str
    consolidated: bool
    consolidated_to: list[str]
    last_consolidated: str | None
    tags: list[str]
    entities: list[str]
    relations: list[dict]
    contradicted_by: list[str]
    supersedes: list[str]
    review_needed: bool
    archived: bool
    current_strength: float | None = None


class RetrievalResult(BaseModel):
    memory: MemoryResponse
    score: float
    score_breakdown: dict


class HealthOverview(BaseModel):
    total_count: int
    layer_counts: dict
    avg_strength: float
    endangered_count: int
    low_confidence_count: int
    active_conflicts: int
    last_consolidation: str | None
    archived_count: int


class PhaseResult(BaseModel):
    action: str
    affected_count: int


class ConsolidationReport(BaseModel):
    timestamp: str
    phases_executed: list[str]
    phase_results: dict[str, list[PhaseResult]]
    memory_changes: int


# ── 辅助函数 ──

def row_to_response(row: dict) -> MemoryResponse:
    """将数据库行转为 MemoryResponse，解析 JSON 字段。"""
    def safe_json(v, default):
        if v is None:
            return default
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return default

    return MemoryResponse(
        id=row["id"],
        type=row["type"],
        layer=row["layer"],
        title=row["title"],
        content=row["content"],
        importance=row["importance"],
        novelty=row["novelty"],
        failure_cost=row["failure_cost"],
        goal_relevance=row["goal_relevance"],
        emotion_weight=row["emotion_weight"],
        surprise_score=row["surprise_score"],
        confidence=row["confidence"],
        source=row["source"],
        corroboration_count=row["corroboration_count"],
        access_count=row["access_count"],
        last_accessed=row["last_accessed"],
        decay_rate=row["decay_rate"],
        half_life_days=row["half_life_days"],
        created=row["created"],
        consolidated=bool(row["consolidated"]),
        consolidated_to=safe_json(row["consolidated_to"], []),
        last_consolidated=row["last_consolidated"],
        tags=safe_json(row["tags"], []),
        entities=safe_json(row["entities"], []),
        relations=safe_json(row["relations"], []),
        contradicted_by=safe_json(row["contradicted_by"], []),
        supersedes=safe_json(row["supersedes"], []),
        review_needed=bool(row["review_needed"]),
        archived=bool(row["archived"]),
        current_strength=None,
    )
