"""页面读取工具状态、发起后台重新检测；不会让聊天等待 MCP。"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.agent.mcp_client import mcp_registry

router = APIRouter()


@router.get("/tools/status")
async def tools_status():
    return {"code": 200, "message": "success", "data": mcp_registry.status()}


@router.post("/tools/refresh")
async def refresh_tools():
    await mcp_registry.start()
    mcp_registry.request_refresh()
    return JSONResponse(status_code=202, content={
        "code": 202, "message": "工具检测已提交，知识库问答可继续使用",
    })
