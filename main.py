"""Brain Memory System — FastAPI 入口。"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from models.database import init_db

app = FastAPI(title="Brain Memory System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await init_db()


# 延迟导入路由，避免循环依赖
from routers.memory_router import router as memory_router
from routers.retrieval_router import router as retrieval_router
from routers.consolidation_router import router as consolidation_router
from routers.health_router import router as health_router
from routers.dashboard_router import router as dashboard_router

app.include_router(memory_router)
app.include_router(retrieval_router)
app.include_router(consolidation_router)
app.include_router(health_router)
app.include_router(dashboard_router)

# 静态文件挂载必须放在最后
app.mount("/", StaticFiles(directory="static", html=True), name="static")
