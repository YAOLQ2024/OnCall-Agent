"""验证真实调用只执行一次，追踪节点保留结构，测试不发送远程 trace。"""

import os
from unittest.mock import Mock

import pytest
from langchain_core.documents import Document
from langchain_core.stores import InMemoryStore
from langchain_milvus import Milvus
from langsmith import tracing_context
from pydantic import ValidationError

from app.config import Settings
from app.core.tracing import configure_langsmith
from app.services.traced_stores import TracedDocumentStore, TracedMilvus


def test_langsmith_settings_are_exported_without_exposing_key(monkeypatch):
    names = (
        "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGSMITH_PROJECT",
        "LANGSMITH_ENDPOINT", "LANGSMITH_API_KEY", "LANGSMITH_WORKSPACE_ID",
        "LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS",
    )
    # 预先登记原值，让 monkeypatch 在测试结束后恢复真实进程环境。
    for name in names:
        monkeypatch.setenv(name, "before-test")
    settings = Settings(
        _env_file=None, langsmith_tracing=True, langsmith_api_key="unit-test-secret",
        langsmith_project="test-project", langsmith_endpoint="https://example.invalid",
        langsmith_workspace_id="test-workspace",
        langsmith_hide_inputs=False, langsmith_hide_outputs=False,
    )
    configure_langsmith(settings)
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "unit-test-secret"
    assert os.environ["LANGSMITH_PROJECT"] == "test-project"
    assert os.environ["LANGSMITH_WORKSPACE_ID"] == "test-workspace"
    assert "unit-test-secret" not in repr(settings.langsmith_api_key)


def test_enabling_tracing_without_key_has_clear_error():
    settings = Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="")
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        configure_langsmith(settings)


def test_child_trace_calls_native_search_once_and_returns_same_documents(monkeypatch):
    child = Document(page_content="命中的子文档", metadata={"parent_id": "parent-1"})
    search = Mock(return_value=[child])

    def native_search(self, query, k=4, **kwargs):
        return search(query, k=k, **kwargs)

    monkeypatch.setattr(Milvus, "similarity_search", native_search)
    # 只测试转发与 trace，不初始化 Milvus 网络连接。
    store = object.__new__(TracedMilvus)
    runs = []
    with tracing_context(enabled="local"):
        result = store.similarity_search(
            "Pending", k=3, expr="filter", langsmith_extra={"on_end": runs.append}
        )
    search.assert_called_once_with("Pending", k=3, expr="filter")
    assert result[0] is child
    assert runs[0].name == "rag.child_search"
    assert runs[0].outputs["documents"][0]["metadata"]["parent_id"] == "parent-1"


def test_parent_trace_records_lookup_keys_and_full_parent_document():
    backing = InMemoryStore()
    parent = Document(page_content="完整父文档上下文", metadata={"source": "test.md"})
    backing.mset([("parent-1", parent)])
    store = TracedDocumentStore(backing)
    runs = []
    with tracing_context(enabled="local"):
        result = store.mget(["parent-1"], langsmith_extra={"on_end": runs.append})
    assert result[0].page_content == parent.page_content
    assert result[0].id == "parent-1"
    assert parent.id is None
    assert runs[0].name == "rag.parent_fetch"
    assert runs[0].inputs["keys"] == ["parent-1"]
    assert runs[0].outputs["documents"][0]["page_content"] == parent.page_content
    assert runs[0].outputs["documents"][0]["id"] == "parent-1"


def test_trace_preserves_errors_instead_of_leaving_pending_run(monkeypatch):
    backing = InMemoryStore()
    monkeypatch.setattr(backing, "mget", Mock(side_effect=RuntimeError("父存储读取失败")))
    store = TracedDocumentStore(backing)
    runs = []
    with tracing_context(enabled="local"):
        with pytest.raises(RuntimeError, match="父存储读取失败"):
            store.mget(["parent-1"], langsmith_extra={"on_end": runs.append})
    assert "父存储读取失败" in runs[0].error
    assert runs[0].end_time is not None


@pytest.mark.asyncio
async def test_knowledge_search_api_returns_service_parent_documents(monkeypatch):
    from app.api import knowledge

    parent = Document(page_content="返回的父文档", metadata={"source": "test.md"})
    retrieve = Mock(return_value=[parent])
    monkeypatch.setattr(knowledge.parent_document_service, "retrieve", retrieve)
    response = await knowledge.search_knowledge(
        knowledge.KnowledgeSearchRequest(query="Pending", top_k=4)
    )
    retrieve.assert_called_once_with("Pending", 4)
    assert response["data"]["count"] == 1
    assert response["data"]["documents"][0]["page_content"] == parent.page_content


def test_knowledge_search_api_rejects_blank_query():
    from app.api.knowledge import KnowledgeSearchRequest

    with pytest.raises(ValidationError):
        KnowledgeSearchRequest(query="   ")
