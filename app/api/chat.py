from app.api.session_guard import guard_session
"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from app.agent.compactor import estimate_message_tokens
from app.config import config
from app.core.llm_factory import model_error_message
from app.models.request import ChatRequest, ClearRequest
from app.models.response import SessionInfoResponse, ApiResponse
from app.services.rag_agent_service import rag_agent_service
from app.agent.supervisor import supervisor_router, IntentType, PURE_GREETING_PATTERN
from app.storage.session_db import session_db_manager, SessionOwnershipError
from app.api.auth import CurrentUser
from app.models.memory_runtime import MemoryRunContext, memory_data_messages, memory_policy
from app.services.memory_service import memory_service
from loguru import logger

router = APIRouter()


def require_session(session_id: str, uid: str):
    if session_db_manager.get_session(session_id, uid) is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")


def ensure_session(session_id: str, uid: str):
    try:
        session_db_manager.create_or_touch_session(session_id, user_id=uid)
    except SessionOwnershipError:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")


def general_chat_messages(session_id: str, uid: str, memory: MemoryRunContext | None = None):
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    history = session_db_manager.get_messages(session_id, uid)
    system = SystemMessage(content="你是智能 OnCall 运维系统的助手。礼貌回应日常问候或系统能力咨询，默认简洁。" + memory_policy(memory))
    messages = []
    remaining = config.chat_history_estimated_tokens
    for record in reversed(history):
        if record.role not in ("user", "assistant"):
            continue
        message_type = HumanMessage if record.role == "user" else AIMessage
        message = message_type(content=record.content)
        cost = estimate_message_tokens(message)
        # Always retain the current user message, even when it fills the history window.
        if messages and cost > remaining:
            break
        messages.append(message)
        remaining -= cost
    return [system, *memory_data_messages(memory), *reversed(messages)]


@router.post("/chat")
@guard_session
async def chat(request: ChatRequest, uid: CurrentUser):
    ensure_session(request.id, uid)
    """快速对话接口 (集成 Supervisor 1+2+3 任务路由)"""
    try:
        snapshot = memory_service.capture_automatic(uid)
        user_message_id = session_db_manager.record_message(request.id, "user", request.question, user_id=uid)
        logger.info(f"[会话 {request.id}] 收到快速对话请求 (用户: {uid}): {request.question}")

        # 1. 任务主管路由决策 (1+2+3 漏斗式分流)
        route_decision = await supervisor_router.route(request.question)
        route_data = route_decision.model_dump()
        execution_trace = [{"type": "route", "data": route_data}]
        logger.info(
            f"[会话 {request.id}] Supervisor 路由决策: {route_decision.intent.value} "
            f"(来源: {route_decision.source}, 耗时: {route_decision.latency_ms}ms)"
        )

        memory = await memory_service.retrieval.prepare(
            uid, request.id, request.question, general=route_decision.intent == IntentType.GENERAL_CHAT,
            explicit=request.memory_scope,
        )

        # 2. 依据路由决策分发至对应下游 Worker
        if route_decision.intent == IntentType.AIOPS_DIAGNOSIS:
            from app.services.aiops_service import aiops_service
            answer = "【AIOps 线上故障排查已介入】\n"
            async for event in aiops_service.execute(request.question, session_id=request.id, user_id=uid, memory_context=memory):
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message", "AIOps 执行失败"))
                if event.get("type") == "complete":
                    answer = event.get("response", answer)
                elif event.get("type") in {"plan", "step_complete"}:
                    execution_trace.append({"type": "aiops_progress", "data": event})
        elif route_decision.intent == IntentType.GENERAL_CHAT and PURE_GREETING_PATTERN.match(request.question.strip()):
            from app.core.llm_factory import llm_factory
            model = llm_factory.create_chat_model(temperature=0.7, streaming=False)
            resp = await model.ainvoke(general_chat_messages(request.id, uid, memory))
            answer = str(resp.content)
        else:
            answer = await rag_agent_service.query(
                request.question,
                session_id=request.id, user_id=uid, memory_context=memory,
            )

        jobs = memory_service.save_turn(
            session_db_manager, uid, request.id, user_message_id, request.question, answer, snapshot,
            diagnostic=route_decision.intent == IntentType.AIOPS_DIAGNOSIS,
            execution_trace=execution_trace,
        )
        logger.info(f"[会话 {request.id}] 快速对话完成并已持久化")

        return {
            "code": 200,
            "message": "success",
            "data": {
                "success": True,
                "answer": answer,
                "route": route_data,
                "execution_trace": execution_trace,
                **({"memory": memory.public()} if memory else {}),
                **jobs,
                "errorMessage": None
            }
        }

    except Exception as e:
        logger.error(f"对话接口错误: {e}")
        status_code = 500
        return JSONResponse(status_code=status_code, content={
            "code": status_code,
            "message": "error",
            "data": {
                "success": False,
                "answer": None,
                "errorMessage": model_error_message(e)
            }
        })


@router.post("/chat_stream")
@guard_session
async def chat_stream(request: ChatRequest, uid: CurrentUser):
    ensure_session(request.id, uid)
    """流式对话接口（基于 Supervisor 路由 + SSE，先推送 route 事件）"""
    logger.info(f"[会话 {request.id}] 收到流式对话请求: {request.question}")

    async def event_generator():
        try:
            snapshot = memory_service.capture_automatic(uid)
            user_message_id = session_db_manager.record_message(request.id, "user", request.question, user_id=uid)
            execution_trace = []

            def save(answer, *, diagnostic=False):
                return memory_service.save_turn(session_db_manager, uid, request.id, user_message_id,
                                                request.question, answer, snapshot, diagnostic=diagnostic,
                                                execution_trace=execution_trace)

            # 1. 任务主管路由决策 (1+2+3 漏斗分流)
            route_decision = await supervisor_router.route(request.question)
            logger.info(
                f"[会话 {request.id}] Supervisor 流式路由决策: {route_decision.intent.value} "
                f"(来源: {route_decision.source}, 耗时: {route_decision.latency_ms}ms)"
            )

            # 向前端推送首个流式路由决策事件
            route_event = {"type": "route", "data": route_decision.model_dump()}
            execution_trace.append(route_event)
            yield {
                "event": "message",
                "data": json.dumps(route_event, ensure_ascii=False)
            }

            memory = await memory_service.retrieval.prepare(
                uid, request.id, request.question, general=route_decision.intent == IntentType.GENERAL_CHAT,
                explicit=request.memory_scope,
            )
            if memory:
                yield {"event": "message", "data": json.dumps(
                    {"type": "memory_recall", "data": memory.public()}, ensure_ascii=False,
                )}

            # 2. 依据路由决策执行分流生成
            if route_decision.intent == IntentType.AIOPS_DIAGNOSIS:
                from app.services.aiops_service import aiops_service
                final_answer = ""
                async for event in aiops_service.execute(request.question, session_id=request.id, user_id=uid, memory_context=memory):
                    ev_type = event.get("type", "step")
                    if ev_type == "error":
                        raise RuntimeError(event.get("message", "AIOps 执行失败"))
                    if ev_type == "complete":
                        final_answer = event.get("response", "")
                    elif ev_type in {"plan", "step_complete", "status"}:
                        progress_event = {"type": "aiops_progress", "data": event}
                        if ev_type != "status":
                            execution_trace.append(progress_event)
                        yield {
                            "event": "message",
                            "data": json.dumps(progress_event, ensure_ascii=False),
                        }
                jobs = save(final_answer, diagnostic=True)
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "done",
                        "data": {"answer": final_answer, "tool_calls": [], **jobs}
                    }, ensure_ascii=False)
                }

            elif route_decision.intent == IntentType.GENERAL_CHAT and PURE_GREETING_PATTERN.match(request.question.strip()):
                from app.core.llm_factory import llm_factory
                model = llm_factory.create_chat_model(temperature=0.7, streaming=True)
                full_text = ""
                async for chunk in model.astream(general_chat_messages(request.id, uid, memory)):
                    if chunk.content:
                        full_text += str(chunk.content)
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "content",
                                "data": str(chunk.content)
                            }, ensure_ascii=False)
                        }
                jobs = save(full_text)
                yield {
                    "event": "message",
                    "data": json.dumps({
                        "type": "done",
                        "data": {"answer": full_text, "tool_calls": [], **jobs}
                    }, ensure_ascii=False)
                }

            else:
                rag_full_answer = ""
                async for chunk in rag_agent_service.query_stream(request.question, session_id=request.id, user_id=uid, memory_context=memory):
                    chunk_type = chunk.get("type", "unknown")
                    chunk_data = chunk.get("data", None)

                    # 处理调试类型消息（新增）
                    if chunk_type == "debug":
                        debug_event = {"type": "debug", "node": chunk.get("node", "unknown"),
                                       "message_type": chunk.get("message_type", "unknown")}
                        execution_trace.append(debug_event)
                        yield {
                            "event": "message",
                            "data": json.dumps(debug_event, ensure_ascii=False)
                        }
                    elif chunk_type == "tool_call":
                        tool_data = chunk_data or {}
                        execution_trace.append({"type": "tool_call", "data": {
                            "tool": tool_data.get("tool"), "status": tool_data.get("status"),
                            "query": tool_data.get("query"), "details": tool_data.get("details"),
                        }})
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "tool_call",
                                "data": chunk_data
                            }, ensure_ascii=False)
                        }
                    elif chunk_type == "search_results":
                        execution_trace.append({"type": "search_results", "data": chunk_data})
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "search_results",
                                "data": chunk_data
                            }, ensure_ascii=False)
                        }
                    elif chunk_type == "compaction":
                        compaction = chunk_data or {}
                        execution_trace.append({"type": "compaction", "data": {
                            "stage": compaction.get("stage"), "message": compaction.get("message"),
                            "details": {"final_tokens": (compaction.get("details") or {}).get("final_tokens")},
                        }})
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "compaction",
                                "data": chunk_data
                            }, ensure_ascii=False)
                        }
                    elif chunk_type == "content":
                        rag_full_answer += str(chunk_data) if chunk_data else ""
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "content",
                                "data": chunk_data
                            }, ensure_ascii=False)
                        }
                    elif chunk_type == "complete":
                        answer = (chunk_data or {}).get("answer") or rag_full_answer
                        jobs = save(answer) if answer else {}
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "type": "done",
                                "data": {**(chunk_data or {}), "answer": answer, **jobs}
                            }, ensure_ascii=False)
                        }
                    elif chunk_type == "error":
                        # Let the outer handler emit one terminal error event.
                        raise RuntimeError(str(chunk_data))

            logger.info(f"[会话 {request.id}] 流式对话完成")

        except Exception as e:
            logger.error(f"流式对话接口错误: {e}")
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "data": model_error_message(e)
                }, ensure_ascii=False)
            }

    return EventSourceResponse(event_generator())


@router.post("/chat/clear", response_model=ApiResponse)
@guard_session
async def clear_session(request: ClearRequest, uid: CurrentUser):
    require_session(request.session_id, uid)
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        success = rag_agent_service.clear_session(request.session_id, user_id=uid)
        logger.info(f"清空会话: {request.session_id}, 结果: {success}")

        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None
        )

    except Exception as e:
        logger.error(f"清空会话错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str, uid: CurrentUser) -> SessionInfoResponse:
    require_session(session_id, uid)
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        history = rag_agent_service.get_session_history(session_id, user_id=uid)

        return SessionInfoResponse(
            session_id=session_id,
            message_count=len(history),
            history=history
        )

    except Exception as e:
        logger.error(f"获取会话信息错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))
