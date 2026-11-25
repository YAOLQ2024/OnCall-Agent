from app.api.session_guard import guard_session
"""持久化会话管理接口 (Session Management API)。

提供完整的会话元数据管理、历史对话恢复与级联清理。
按经过验证的演示用户 ID 过滤会话与历史消息。
"""

import uuid
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from loguru import logger

from app.storage.session_db import session_db_manager, SessionOwnershipError
from app.api.auth import CurrentUser

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: Optional[str] = "新排障会话"
    session_id: Optional[str] = None


class UpdateTitleRequest(BaseModel):
    title: str


@router.get("")
async def list_user_sessions(uid: CurrentUser) -> Dict[str, Any]:
    """获取当前登录用户的全部历史排障会话列表（按最近更新时间倒序）。"""
    sessions = session_db_manager.list_sessions(user_id=uid)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "user_id": uid,
            "sessions": [s.model_dump() for s in sessions],
        }
    }


@router.post("")
async def create_new_session(
    req: CreateSessionRequest,
    uid: CurrentUser,
) -> Dict[str, Any]:
    """创建一个新的排障会话。"""
    s_id = req.session_id or f"sess_{uuid.uuid4().hex[:12]}"
    try:
        meta = session_db_manager.create_or_touch_session(s_id, req.title or "新排障会话", uid)
    except SessionOwnershipError:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    logger.info(f"创建新持久化会话: {s_id} (用户: {uid})")
    return {
        "code": 200,
        "message": "success",
        "data": meta.model_dump(),
    }


@router.get("/{session_id}")
async def get_session_detail(
    session_id: str,
    uid: CurrentUser,
) -> Dict[str, Any]:
    """获取指定会话的详情及持久化对话历史。"""
    meta = session_db_manager.get_session(session_id, user_id=uid)
    if not meta:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    messages = session_db_manager.get_messages(session_id, user_id=uid)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "session": meta.model_dump(),
            "messages": [m.model_dump() for m in messages],
        }
    }


@router.get("/{session_id}/message-references")
async def list_message_references(session_id: str, uid: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        messages = await run_in_threadpool(session_db_manager.get_message_references, session_id, uid)
    except SessionOwnershipError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    return {"code": 200, "message": "success", "data": {"messages": messages}}


@router.put("/{session_id}/title")
async def update_session_title(
    session_id: str,
    req: UpdateTitleRequest,
    uid: CurrentUser,
) -> Dict[str, Any]:
    """修改会话标题。"""
    ok = session_db_manager.update_session_title(session_id, req.title, user_id=uid)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无权修改")
    return {"code": 200, "message": "success", "data": {"session_id": session_id, "title": req.title}}


@router.delete("/{session_id}")
@guard_session
async def delete_session(
    session_id: str,
    uid: CurrentUser,
) -> Dict[str, Any]:
    """级联删除会话元数据、持久化消息及 LangGraph 状态机 Checkpoint。"""
    if not session_db_manager.delete_session(session_id, user_id=uid):
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")

    logger.info(f"已级联清理会话 {session_id} 及其所有持久化 Checkpoint (用户: {uid})")
    return {"code": 200, "message": "success", "data": {"session_id": session_id, "deleted": True}}
