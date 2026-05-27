"""Context 路由 — 自动上下文加载。

POST /api/v1/context
  输入当前对话上下文，返回相关记忆 + 格式化上下文块（可直接注入 prompt）。
  
  请求体:
    {
      "query": "用户正在讨论 MCP 自动加载问题",
      "current_goal": "修复 Hermes Agent 的 MCP 连接",
      "top_k": 5
    }
  
  响应:
    {
      "context_block": "=== 相关记忆 ===\n...",
      "should_inject": true,
      "memories": [...],
      "high_relevance": [...]
    }
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field
from services.context_load import context_load

router = APIRouter(prefix="/api/v1", tags=["context"])


class ContextRequest(BaseModel):
    query: str = Field(..., description="当前对话上下文（用户消息 + 任务摘要）", min_length=1)
    current_goal: str | None = Field(None, description="当前目标")
    current_risk: str | None = Field(None, description="当前风险/关注点")
    top_k: int = Field(5, ge=1, le=20)
    min_relevance: float = Field(0.15, ge=0.0, le=1.0)


class ContextResponse(BaseModel):
    context_block: str
    should_inject: bool
    memories: list[dict]
    high_relevance: list[dict]
    total_available: int
    query_summary: str


@router.post("/context", response_model=ContextResponse)
async def load_context(req: ContextRequest):
    """加载与当前对话上下文相关的记忆。

    Agent 应在以下时机调用：
    1. 会话开始时 — 加载最近的重要记忆
    2. 话题转换时 — 搜索新话题的相关记忆
    3. 用户引用过去时 — "上次那个...""之前我们..."
    4. 做重要决策前 — 检查相关历史
    5. 遇到不确定性时 — 搜索是否有已知答案
    """
    return await context_load(
        query=req.query,
        current_goal=req.current_goal,
        current_risk=req.current_risk,
        top_k=req.top_k,
        min_relevance=req.min_relevance,
    )
