"""父子召回的子文档向量存储；集合结构由 LangChain Milvus 集成管理。"""

import asyncio
from threading import RLock

from langchain_milvus import Milvus
from pymilvus import AsyncMilvusClient
from starlette.concurrency import run_in_threadpool

from app.config import config
from app.services.traced_stores import TracedMilvus


class VectorStoreManager:
    def __init__(self) -> None:
        self._vector_store: Milvus | None = None
        self._async_client: AsyncMilvusClient | None = None
        self._lock = RLock()
        self._lifecycle_lock = asyncio.Lock()

    def get_vector_store(self) -> Milvus:
        """请求线程只读取已初始化的存储，不在线程池中创建异步客户端。"""
        with self._lock:
            if self._vector_store is None:
                raise RuntimeError("向量存储尚未初始化，应在应用启动时 await vector_store_manager.initialize()")
            return self._vector_store

    async def initialize(self) -> Milvus:
        """在应用运行中的事件循环内构造集成，完成同步和异步客户端初始化。"""
        async with self._lifecycle_lock:
            with self._lock:
                if self._vector_store is not None:
                    return self._vector_store
            from app.services.vector_embedding_service import vector_embedding_service

            store = None
            async_client = None
            try:
                # 0.3.3 的同步构造函数也会创建 AsyncMilvusClient。
                # 这一步必须在事件循环线程执行；同步业务操作仍在线程池中运行。
                store = TracedMilvus(
                    embedding_function=vector_embedding_service,
                    collection_name=config.rag_collection_name,
                    connection_args={
                        "host": config.milvus_host,
                        "port": config.milvus_port,
                        "timeout": config.milvus_timeout / 1000,
                    },
                    auto_id=True,
                    enable_dynamic_field=True,
                    drop_old=False,
                    consistency_level="Strong",
                    text_field="content",
                    vector_field="vector",
                    # langchain-milvus 0.3.3 在首次建集合时会把构造级 timeout
                    # 重复转发给 insert。保持其默认 None，操作超时由调用方传入。
                )
                async_client = store.aclient
                if store.col is not None:
                    fields = {field.name: field for field in store.col.schema.fields}
                    vector = fields.get("vector")
                    if vector is None or vector.params.get("dim") != config.qwen_embedding_dimensions:
                        raise RuntimeError(
                            "父子向量集合维度与当前 embedding 配置不一致，请配置新集合并重建索引"
                        )
                    if not store.col.schema.enable_dynamic_field:
                        raise RuntimeError("父子召回需要支持动态 metadata 的独立集合，请配置新集合")
            except BaseException as error:
                if store is not None:
                    try:
                        await self._close_clients(store, async_client)
                    except Exception as cleanup_error:
                        error.add_note(f"初始化失败后的 Milvus 客户端清理也发生错误：{cleanup_error}")
                raise

            with self._lock:
                self._vector_store = store
                self._async_client = async_client
            return store

    @staticmethod
    async def _close_clients(store: Milvus, async_client: AsyncMilvusClient | None) -> None:
        try:
            if async_client is not None:
                await async_client.close()
        finally:
            await run_in_threadpool(store.client.close)

    async def close(self) -> None:
        """在所属事件循环内关闭异步客户端，同步客户端在线程池关闭。"""
        async with self._lifecycle_lock:
            with self._lock:
                store, async_client = self._vector_store, self._async_client
                self._vector_store = None
                self._async_client = None
            if store is not None:
                await self._close_clients(store, async_client)


vector_store_manager = VectorStoreManager()
