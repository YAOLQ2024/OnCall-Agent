"""向量存储客户端在事件循环中初始化/关闭，业务线程只复用缓存。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import vector_store_manager as module


def fake_store():
    return SimpleNamespace(
        col=None, client=SimpleNamespace(close=Mock()),
        aclient=SimpleNamespace(close=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_initialization_has_running_loop_and_worker_threads_reuse_cached_store(monkeypatch):
    owner_loop = asyncio.get_running_loop()
    store = fake_store()

    def build(**kwargs):
        assert asyncio.get_running_loop() is owner_loop
        return store

    factory = Mock(side_effect=build)
    monkeypatch.setattr(module, "TracedMilvus", factory)
    manager = module.VectorStoreManager()
    try:
        assert await manager.initialize() is store
        assert await manager.initialize() is store
        assert await asyncio.to_thread(manager.get_vector_store) is store
        factory.assert_called_once()
    finally:
        await manager.close()
    store.aclient.close.assert_awaited_once()
    store.client.close.assert_called_once()


@pytest.mark.asyncio
async def test_async_client_is_closed_on_its_owner_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    store = fake_store()

    async def close_async():
        assert asyncio.get_running_loop() is loop

    store.aclient.close.side_effect = close_async
    monkeypatch.setattr(module, "TracedMilvus", Mock(return_value=store))
    manager = module.VectorStoreManager()
    await manager.initialize()
    await manager.close()
    await manager.close()
    store.aclient.close.assert_awaited_once()
    store.client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_invalid_schema_closes_both_clients_and_does_not_publish_store(monkeypatch, close_fails):
    store = fake_store()
    store.col = SimpleNamespace(schema=SimpleNamespace(
        fields=[SimpleNamespace(name="vector", params={"dim": -1})], enable_dynamic_field=True
    ))
    if close_fails:
        store.aclient.close.side_effect = RuntimeError("清理失败")
    monkeypatch.setattr(module, "TracedMilvus", Mock(return_value=store))
    manager = module.VectorStoreManager()
    with pytest.raises(RuntimeError, match="维度"):
        await manager.initialize()
    store.aclient.close.assert_awaited_once()
    store.client.close.assert_called_once()
    with pytest.raises(RuntimeError, match="尚未初始化"):
        manager.get_vector_store()


@pytest.mark.asyncio
async def test_async_close_failure_still_closes_sync_client(monkeypatch):
    store = fake_store()
    store.aclient.close.side_effect = RuntimeError("异步关闭失败")
    monkeypatch.setattr(module, "TracedMilvus", Mock(return_value=store))
    manager = module.VectorStoreManager()
    await manager.initialize()
    with pytest.raises(RuntimeError, match="异步关闭失败"):
        await manager.close()
    store.client.close.assert_called_once()


def test_getter_does_not_lazily_construct_clients_in_sync_context(monkeypatch):
    factory = Mock(side_effect=AssertionError("不应在线程池或普通同步上下文中创建异步客户端"))
    monkeypatch.setattr(module, "TracedMilvus", factory)
    manager = module.VectorStoreManager()
    with pytest.raises(RuntimeError, match="尚未初始化"):
        manager.get_vector_store()
    factory.assert_not_called()
