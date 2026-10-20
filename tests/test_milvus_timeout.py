"""覆盖原生 Milvus 首次建集合的插入路径；不连接数据库或嵌入接口。"""

from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.services.traced_stores import TracedMilvus


@pytest.mark.parametrize("collection_exists", [False, True])
@pytest.mark.asyncio
async def test_native_insert_receives_timeout_once(monkeypatch, collection_exists):
    from app.services import vector_store_manager as module

    inserted = []
    client = Mock()
    client.has_collection.return_value = collection_exists

    # 使用明确的 timeout 形参，重复转发会像真实 MilvusClient 一样抛出 TypeError。
    def insert(collection_name, data, timeout=None, **kwargs):
        inserted.append((collection_name, data, timeout))
        return {"ids": [101]}

    client.insert = insert
    store = object.__new__(TracedMilvus)
    store._milvus_client = client
    store._async_milvus_client = SimpleNamespace(close=AsyncMock())
    store.collection_name = "test_children"
    store.embedding_func = Mock()
    store.embedding_func.embed_documents.return_value = [[0.1, 0.2]]
    store.auto_id = True
    store.partition_names = None
    store.replica_number = 1
    store.timeout = None
    store._prepare_insert_list = Mock(return_value=[{"content": "子文档"}])
    store._init = Mock()

    def build_store(**kwargs):
        # 不调用真实构造函数；验证应用不再传入触发缺陷的构造级 timeout。
        assert kwargs.get("timeout") is None
        assert kwargs["connection_args"]["timeout"] == module.config.milvus_timeout / 1000
        return store

    monkeypatch.setattr(module, "TracedMilvus", build_store)
    monkeypatch.setattr(TracedMilvus, "col", property(lambda self: None))
    manager = module.VectorStoreManager()
    managed = await manager.initialize()
    try:
        ids = managed.add_documents(
            [Document(page_content="子文档", metadata={"parent_id": "parent-1"})],
            timeout=7.5,
        )
    finally:
        await manager.close()
    assert ids == [101]
    assert len(inserted) == 1
    assert inserted[0][2] == 7.5
    store.embedding_func.embed_documents.assert_called_once_with(["子文档"])
    if collection_exists:
        store._init.assert_not_called()
    else:
        store._init.assert_called_once()
        assert "timeout" not in store._init.call_args.kwargs
