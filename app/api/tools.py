"""页面读取工具状态、发起后台重新检测；支持外部 mcp_servers.json 的 CRUD 动态配置。"""

import json
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.mcp_client import mcp_registry
from app.config import config

router = APIRouter()


class MCPConfigUpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, description="原始 JSON 字符串")
    config: Optional[Dict[str, Any]] = Field(default=None, description="结构化配置对象")


class MCPServerUpdateRequest(BaseModel):
    name: str = Field(..., description="服务名称标识")
    server: Dict[str, Any] = Field(..., description="服务配置，包含 transport、command、args 或 url")


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


@router.get("/tools/config")
async def get_mcp_config():
    """获取当前的外部 mcp_servers.json 配置内容"""
    file_path = config.mcp_servers_file
    raw_content = ""
    if file_path.exists():
        try:
            raw_content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raw_content = ""
    parsed = config.read_custom_mcp_config()
    return {
        "code": 200,
        "message": "success",
        "data": {
            "raw": raw_content or json.dumps(parsed, ensure_ascii=False, indent=2),
            "parsed": parsed,
            "file_path": str(file_path),
            "status": mcp_registry.status(),
        }
    }


@router.post("/tools/config")
async def save_mcp_config(req: MCPConfigUpdateRequest):
    """保存或全量更新 mcp_servers.json，并热重载 MCP 服务"""
    target_dict = {}
    if req.content is not None:
        try:
            parsed = json.loads(req.content)
            if not isinstance(parsed, dict):
                raise ValueError("JSON 根节点必须是一个字典对象 (例如 {'mcpServers': {...}})")
            target_dict = parsed
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无效的 JSON 格式: {str(e)}"
            )
    elif req.config is not None:
        target_dict = req.config
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须提供 content 或 config 参数"
        )

    # 写入文件
    try:
        config.write_custom_mcp_config(target_dict)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入配置文件失败: {str(e)}"
        )

    # 触发 MCP 服务热重载
    updated_status = await mcp_registry.reload_config()

    return {
        "code": 200,
        "message": "MCP 配置已成功保存并热重载",
        "data": updated_status
    }


@router.post("/tools/servers")
async def add_or_update_server(req: MCPServerUpdateRequest):
    """添加或更新单个 MCP 服务配置"""
    raw = config.read_custom_mcp_config()
    # 统一使用 mcpServers 包裹结构（保持与 Cline / Claude Desktop 完全互通）
    if "mcpServers" not in raw or not isinstance(raw["mcpServers"], dict):
        raw = {"mcpServers": raw}

    raw["mcpServers"][req.name] = req.server

    try:
        config.write_custom_mcp_config(raw)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存配置文件失败: {str(e)}"
        )

    updated_status = await mcp_registry.reload_config()
    return {
        "code": 200,
        "message": f"服务 '{req.name}' 配置已更新并重载",
        "data": updated_status
    }


@router.delete("/tools/servers/{server_name}")
async def delete_server(server_name: str):
    """从外部配置中删除指定的 MCP 服务"""
    raw = config.read_custom_mcp_config()
    deleted = False

    if "mcpServers" in raw and isinstance(raw["mcpServers"], dict):
        if server_name in raw["mcpServers"]:
            del raw["mcpServers"][server_name]
            deleted = True
    elif server_name in raw:
        del raw[server_name]
        deleted = True

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未在自定义配置中找到服务 '{server_name}'（内置服务不支持从自定义配置中删除）"
        )

    try:
        config.write_custom_mcp_config(raw)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存配置文件失败: {str(e)}"
        )

    updated_status = await mcp_registry.reload_config()
    return {
        "code": 200,
        "message": f"服务 '{server_name}' 已成功移除并重载",
        "data": updated_status
    }
