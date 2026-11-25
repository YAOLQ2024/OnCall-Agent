"""Private profile and incident management for the authenticated user."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from app.api.auth import CurrentUser
from app.models.memory import (
    ForgetMemoryRequest, Identifier, IncidentConfirm, IncidentCreate, IncidentInvalidate, IncidentPatch,
    IncidentStatus, MemoryReviewRequest, Namespace, ProfilePatch, SessionExtractionRequest,
)
from app.services.memory_service import memory_service
from app.storage.memory_db import MemoryUserNotFoundError, ProfileConflictError
from app.storage.memory_jobs import MemoryBudgetError
from app.storage.memory_incidents import IncidentConflictError, IncidentNotFoundError, IncidentValidationError

router = APIRouter(prefix="/memory", tags=["长期记忆"])


def _check_query(request: Request):
    if request.query_params:
        raise HTTPException(status_code=422, detail="此接口不接受查询参数，请使用当前登录身份")


@router.get("/profile")
async def get_profile(request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return memory_service.response(await run_in_threadpool(memory_service.get_profile, uid))
    except MemoryUserNotFoundError:
        raise HTTPException(status_code=404, detail="用户不存在")


@router.patch("/profile")
async def patch_profile(patch: ProfilePatch, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return memory_service.response(await run_in_threadpool(memory_service.patch_profile, uid, patch))
    except ProfileConflictError:
        raise HTTPException(status_code=409, detail="画像已在其他页面更新，请重新加载后编辑")
    except MemoryUserNotFoundError:
        raise HTTPException(status_code=404, detail="用户不存在")


class IncidentListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class IncidentSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    limit: int = Field(default=20, ge=1, le=100)
    environment: Identifier | None = None
    cluster: Identifier | None = None
    namespace: Namespace | None = None


class IncidentDeleteQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, le=2**63 - 1)


async def _incident_response(response: Response, operation, *args, code=200, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await run_in_threadpool(operation, *args, **kwargs)
    except MemoryUserNotFoundError:
        raise HTTPException(status_code=404, detail="用户不存在") from None
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except (IncidentConflictError, MemoryBudgetError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except IncidentValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return {"code": code, "message": "success", "data": result}


@router.get("/incidents")
async def list_incidents(query: Annotated[IncidentListQuery, Query()], response: Response, uid: CurrentUser):
    return await _incident_response(response, memory_service.incidents.list, uid, **query.model_dump())


@router.post("/incidents", status_code=201)
async def create_incident(draft: IncidentCreate, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.incidents.create, uid, draft, code=201)


@router.get("/incidents/search")
async def search_incidents(query: Annotated[IncidentSearchQuery, Query()], response: Response, uid: CurrentUser):
    filters = query.model_dump(exclude={"q", "limit"})
    return await _incident_response(response, memory_service.incidents.search, uid, query.q, query.limit, **filters)


@router.post("/incidents/from-session", status_code=202)
async def extract_incident(payload: SessionExtractionRequest, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.enqueue_manual, uid, payload, code=202)


class JobListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


@router.get("/jobs")
async def list_jobs(query: Annotated[JobListQuery, Query()], response: Response, uid: CurrentUser):
    return await _incident_response(response, memory_service.jobs.list, uid, **query.model_dump())


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.jobs.get, uid, job_id)


@router.post("/jobs/{job_id}/review")
async def review_job(job_id: str, review: MemoryReviewRequest, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.jobs.review, uid, job_id, review)


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.jobs.retry, uid, job_id, code=202)


@router.post("/forget")
async def forget_memory(payload: ForgetMemoryRequest, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="请明确确认清除长期记忆")
    return await _incident_response(response, memory_service.jobs.forget, uid, payload.expected_generation)


@router.get("/incidents/{mid}")
async def get_incident(mid: str, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.incidents.get, uid, mid)


@router.patch("/incidents/{mid}")
async def patch_incident(mid: str, patch: IncidentPatch, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.incidents.patch, uid, mid, patch)


@router.post("/incidents/{mid}/confirm")
async def confirm_incident(mid: str, confirmation: IncidentConfirm, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.incidents.confirm, uid, mid, confirmation)


@router.post("/incidents/{mid}/invalidate")
async def invalidate_incident(mid: str, action: IncidentInvalidate, request: Request, response: Response, uid: CurrentUser):
    _check_query(request)
    return await _incident_response(response, memory_service.incidents.invalidate, uid, mid, action)


@router.delete("/incidents/{mid}")
async def withdraw_incident(mid: str, query: Annotated[IncidentDeleteQuery, Query()], response: Response, uid: CurrentUser):
    return await _incident_response(response, memory_service.incidents.withdraw, uid, mid, query.expected_revision)
