"""用原生 ParentDocumentRetriever 验证生命周期；替身不验证向量模型或 Milvus。"""

import json
from io import BytesIO
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore

from app.config import Settings
from app.services.parent_document_service import ParentDocumentService


class RecordingVectorStore(VectorStore):
    """只记录写入、版本过滤和删除；相似度排序不在本单元测试范围内。"""

    def __init__(self):
        self.documents = {}
        self.fail_next_add = False
        self.fail_delete = False

    def add_documents(self, documents, **kwargs):
        ids = []
        for document in documents:
            key = uuid4().hex
            self.documents[key] = document.model_copy(deep=True)
            ids.append(key)
            if self.fail_next_add:
                self.fail_next_add = False
                raise RuntimeError("模拟部分子向量写入后失败")
        return ids

    def similarity_search(self, query, k=4, **kwargs):
        generations = json.loads(kwargs["expr"].split(" in ", 1)[1])
        return [
            document for document in self.documents.values()
            if document.metadata["_index_generation"] in generations
        ][:k]

    def delete(self, ids=None, **kwargs):
        if self.fail_delete:
            return False
        generation = json.loads(kwargs["expr"].split(" == ", 1)[1])
        self.documents = {
            key: document for key, document in self.documents.items()
            if document.metadata["_index_generation"] != generation
        }
        return True

    @classmethod
    def from_texts(cls, texts, embedding, metadatas=None, **kwargs):
        store = cls()
        store.add_documents([
            Document(page_content=text, metadata=metadata)
            for text, metadata in zip(texts, metadatas or [{} for _ in texts], strict=True)
        ])
        return store


@pytest.fixture
def resources(tmp_path):
    settings = Settings(
        _env_file=None,
        rag_storage_path=str(tmp_path), rag_collection_name="test_children",
        rag_parent_chunk_size=500, rag_child_chunk_size=80, rag_child_chunk_overlap=10,
    )
    vectorstore = RecordingVectorStore()
    service = ParentDocumentService(settings, lambda: vectorstore)
    return settings, vectorstore, service


def make_document(text, source="/knowledge/debug.md"):
    return Document(page_content=text, metadata={
        "_source": source, "source": "https://example.com/debug", "title": "调试 Pod",
    })


def test_native_retriever_returns_full_parent_and_deduplicates(resources):
    _, vectorstore, service = resources
    document = make_document("# 调试 Pod\n\n" + "Pending 时先检查事件和资源是否不足。\n\n" * 8)
    result = service.index_document(document)
    assert result["parent_count"] == 1
    assert len(vectorstore.documents) > 1
    ids = {child.metadata["parent_id"] for child in vectorstore.documents.values()}
    assert ids == set(service.docstore.yield_keys())
    parents = service.retrieve("Pending", top_k=100)
    assert len(parents) == 1
    assert parents[0].id in ids
    assert parents[0].page_content == document.page_content
    assert parents[0].metadata["source"] == document.metadata["source"]
    assert "_index_generation" not in document.metadata


def test_milvus_operations_receive_configured_timeout(resources, monkeypatch):
    settings, vectorstore, service = resources
    write = Mock(wraps=vectorstore.add_documents)
    search = Mock(wraps=vectorstore.similarity_search)
    delete = Mock(wraps=vectorstore.delete)
    monkeypatch.setattr(vectorstore, "add_documents", write)
    monkeypatch.setattr(vectorstore, "similarity_search", search)
    monkeypatch.setattr(vectorstore, "delete", delete)
    service.index_document(make_document("验证超时参数的文档"))
    service.retrieve("文档")
    service.delete_source("/knowledge/debug.md")
    seconds = settings.milvus_timeout / 1000
    assert write.call_args.kwargs["timeout"] == seconds
    assert search.call_args.kwargs["timeout"] == seconds
    assert delete.call_args.kwargs["timeout"] == seconds


def test_source_update_replaces_both_layers_and_retains_other_sources(resources):
    _, vectorstore, service = resources
    service.index_document(make_document("旧版排查步骤"))
    old_ids = set(service.docstore.yield_keys())
    service.index_document(make_document("另一个文件", "/knowledge/other.md"))
    service.index_document(make_document("新版排查步骤"))
    parents = service.retrieve("排查", top_k=100)
    assert {parent.page_content for parent in parents} == {"新版排查步骤", "另一个文件"}
    assert all(parent is None for parent in service.docstore.mget(list(old_ids)))
    assert {child.page_content for child in vectorstore.documents.values()} == {
        "新版排查步骤", "另一个文件",
    }


def test_partial_write_failure_keeps_old_version_and_cleans_new_data(resources):
    _, vectorstore, service = resources
    old = service.index_document(make_document("仍然可用的旧版"))
    vectorstore.fail_next_add = True
    with pytest.raises(RuntimeError, match="部分子向量"):
        service.index_document(make_document("不能发布的新版"))
    assert [doc.page_content for doc in service.retrieve("排查")] == ["仍然可用的旧版"]
    assert {doc.metadata["_index_generation"] for doc in vectorstore.documents.values()} == {
        old["generation"],
    }


def test_failed_cleanup_does_not_expose_stale_children_and_retries(resources):
    _, vectorstore, service = resources
    service.index_document(make_document("旧版"))
    vectorstore.fail_delete = True
    service.index_document(make_document("新版"))
    assert len(vectorstore.documents) == 2
    assert [doc.page_content for doc in service.retrieve("排查")] == ["新版"]
    vectorstore.fail_delete = False
    service.initialize()
    assert len(vectorstore.documents) == 1


def test_parent_documents_and_source_manifest_survive_service_recreation(resources):
    settings, vectorstore, service = resources
    service.index_document(make_document("重启后应能取回"))
    restarted = ParentDocumentService(settings, lambda: vectorstore)
    assert [doc.page_content for doc in restarted.retrieve("排查")] == ["重启后应能取回"]


def test_changed_configuration_requires_reindex_and_delete_source_removes_both_layers(resources):
    settings, vectorstore, service = resources
    service.index_document(make_document("原配置文档"))
    changed = ParentDocumentService(
        settings.model_copy(update={"rag_child_chunk_size": 120}), lambda: vectorstore
    )
    assert changed.retrieve("排查") == []
    changed.index_document(make_document("新配置文档"))
    assert [doc.page_content for doc in changed.retrieve("排查")] == ["新配置文档"]
    changed.delete_source("/knowledge/debug.md")
    assert changed.retrieve("排查") == []
    assert not vectorstore.documents
    assert not list(changed.docstore.yield_keys())


def test_index_service_routes_loaded_document_to_parent_service(tmp_path, monkeypatch):
    from app.services import vector_index_service as module

    path = tmp_path / "debug.md"
    path.write_text("# 自动标题\n\n正文", encoding="utf-8")
    index = Mock(return_value={"parent_count": 1})
    monkeypatch.setattr(module.parent_document_service, "index_document", index)
    assert module.vector_index_service.index_single_file(str(path)) == {"parent_count": 1}
    document = index.call_args.args[0]
    assert document.metadata["title"] == "自动标题"
    assert document.metadata["_source"] == path.resolve().as_posix()


def test_agent_knowledge_tool_uses_parent_context(monkeypatch):
    from app.tools import knowledge_tool as module

    parent = make_document("父文档包含的完整排查上下文")
    retrieve = Mock(return_value=[parent])
    monkeypatch.setattr(module.knowledge_retrieval_service, "retrieve", retrieve)
    context = module.retrieve_knowledge.invoke({"query": "Pending"})
    retrieve.assert_called_once_with("Pending", chat_history=[])
    assert parent.page_content in context
    assert parent.metadata["source"] in context


@pytest.mark.asyncio
async def test_upload_reports_index_failure_instead_of_success(tmp_path, monkeypatch):
    from app.api import file as module

    monkeypatch.setattr(module, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        module.vector_index_service, "index_single_file", Mock(side_effect=RuntimeError("不可用"))
    )
    upload = UploadFile(filename="debug.md", file=BytesIO(b"# document\nbody"))
    with pytest.raises(HTTPException) as error:
        await module.upload_file(upload)
    assert error.value.status_code == 500
    assert "索引失败" in error.value.detail
