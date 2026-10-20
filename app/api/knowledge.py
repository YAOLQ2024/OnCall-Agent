"""知识库检索接口，返回父文档；复用 Agent 使用的同一召回服务。"""

from fastapi import APIRouter, HTTPException
from langsmith import traceable
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.services.parent_document_service import parent_document_service

router = APIRouter()


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, description="检索问题")
    top_k: int | None = Field(default=None, ge=1, le=100, description="命中子文档数量上限")


@router.post("/knowledge/search")
@traceable(name="knowledge.search", run_type="chain")
async def search_knowledge(request: KnowledgeSearchRequest):
    try:
        parents = await run_in_threadpool(
            parent_document_service.retrieve, request.query, request.top_k
        )
        return {
            "code": 200,
            "message": "success",
            "data": {
                "count": len(parents),
                "documents": [parent.model_dump(mode="json") for parent in parents],
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"知识库检索失败：{exc}") from exc
