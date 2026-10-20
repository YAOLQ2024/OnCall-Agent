"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
import os

from app.config import config
from loguru import logger
from app.api import chat, health, file, aiops, knowledge, tools
from app.agent.mcp_client import mcp_registry
from app.core.milvus_client import milvus_manager
from app.services.parent_document_service import parent_document_service
from app.services.vector_store_manager import vector_store_manager
from app.core.tracing import flush_langsmith


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    try:
        await run_in_threadpool(milvus_manager.connect)
        await vector_store_manager.initialize()
        await run_in_threadpool(parent_document_service.initialize)
        await mcp_registry.start()
        logger.info("✅ Milvus 连接成功")
        logger.info("=" * 60)
        try:
            yield
        finally:
            await mcp_registry.close()
    finally:
        logger.info("🔌 正在关闭 Milvus 连接...")
        try:
            await vector_store_manager.close()
        finally:
            try:
                await run_in_threadpool(milvus_manager.close)
            finally:
                try:
                    await run_in_threadpool(flush_langsmith)
                except Exception as exc:
                    logger.warning("LangSmith 追踪缓冲区提交失败：{}", exc)
        logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(knowledge.router, prefix="/api", tags=["知识库检索"])
app.include_router(tools.router, prefix="/api", tags=["工具状态"])

# 挂载静态文件
static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        log_level="info"
    )
