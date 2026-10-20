"""知识库检索接口，返回父文档；复用 Agent 使用的同一召回服务。"""

from fastapi import APIRouter, HTTPException
from langsmith import traceable
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from langchain_core.messages import AIMessage, HumanMessage

from app.services.knowledge_retrieval_service import knowledge_retrieval_service

router = APIRouter()


class HistoryMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, max_length=4000, description="检索问题")
    top_k: int | None = Field(default=None, ge=1, le=100, description="每路子文档上限；最终父文档最多 RAG_RERANK_TOP_N 篇")
    chat_history: list[HistoryMessage] = Field(default_factory=list, max_length=20)


@router.post("/knowledge/search")
@traceable(name="knowledge.search", run_type="chain")
async def search_knowledge(request: KnowledgeSearchRequest):
    try:
        parents = await knowledge_retrieval_service.aretrieve(
            request.query, request.top_k, chat_history=[
                (HumanMessage if item.role == "user" else AIMessage)(content=item.content)
                for item in request.chat_history
            ],
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
