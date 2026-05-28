"""Brain Memory System — FastAPI 入口 v3.0。

后台自动运行：评分刷新(30min)、轻量巩固(1h)、完整巩固(2h)、压缩(6h)。
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from models.database import init_db
from config import HOST, PORT

# 日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("brain-memory")

# 调度器停止信号
_stop_event: asyncio.Event | None = None
_scheduler_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化 DB + 调度器，关闭时停止调度器。"""
    global _stop_event, _scheduler_task

    # 启动
    await init_db()
    logger.info("Brain Memory v3.0 — database initialized")

    # 启动后台调度器
    from services.scheduler import run_scheduler
    _stop_event = asyncio.Event()
    _scheduler_task = asyncio.create_task(run_scheduler(_stop_event))
    logger.info("scheduler: background task started")

    yield

    # 关闭
    if _stop_event:
        _stop_event.set()
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Brain Memory v3.0 — shutdown complete")


app = FastAPI(title="Brain Memory System", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)

# 路由
from routers.memory_router import router as memory_router
from routers.retrieval_router import router as retrieval_router
from routers.consolidation_router import router as consolidation_router
from routers.health_router import router as health_router
from routers.dashboard_router import router as dashboard_router
from routers.ingest_router import router as ingest_router
from routers.context_router import router as context_router
from routers.pipeline_router import router as pipeline_router

app.include_router(memory_router)
app.include_router(retrieval_router)
app.include_router(consolidation_router)
app.include_router(health_router)
app.include_router(dashboard_router)
app.include_router(ingest_router)
app.include_router(context_router)
app.include_router(pipeline_router)

# 静态文件挂载必须放在最后
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)